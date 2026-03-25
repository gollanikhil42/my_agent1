import { useEffect, useMemo, useRef, useState } from "react";
import { fetchAuthSession, fetchUserAttributes } from "aws-amplify/auth";

const SESSION_STORAGE_KEY = "session_id";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL;
const ANALYSIS_MAX_RETRIES = 2;
const ANALYST_MEMORY_LIMIT = 6;
const RETRYABLE_STATUS = new Set([429, 502, 503]);
const DEFAULT_FLEET_PAGE_SIZE = 20;
const ANALYSER_SUGGESTIONS = [
  "Give me a summary of all sessions in this time window and highlight any issues.",
  "Which sessions are the slowest and what is causing the latency?",
  "Which sessions had the most errors and what patterns do you see?",
  "List all users and show their sessions along with any issues they faced.",
  "Analyze the most problematic session in detail and explain what went wrong.",
];
const LOOKBACK_OPTIONS = [
  { value: "1", label: "1 hour" },
  { value: "6", label: "6 hours" },
  { value: "12", label: "12 hours" },
  ...Array.from({ length: 30 }, (_, idx) => {
    const day = idx + 1;
    return { value: String(day * 24), label: `${day} day${day === 1 ? "" : "s"}` };
  }),
  { value: "overall", label: "Overall" },
];

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function postWithRetry(url, options, maxRetries = ANALYSIS_MAX_RETRIES, signal = null) {
  let lastResponse = null;
  let lastError = null;

  for (let attempt = 0; attempt <= maxRetries; attempt += 1) {
    if (signal?.aborted) {
      throw new DOMException("Request aborted", "AbortError");
    }

    try {
      const response = await fetch(url, { ...options, signal });
      lastResponse = response;
      if (!RETRYABLE_STATUS.has(response.status) || attempt === maxRetries) {
        return response;
      }
    } catch (error) {
      if (signal?.aborted || error?.name === "AbortError") {
        throw error;
      }
      lastError = error;
      if (attempt === maxRetries) {
        throw error;
      }
    }

    if (signal?.aborted) {
      throw new DOMException("Request aborted", "AbortError");
    }
    await sleep(900 * (attempt + 1));
  }

  if (lastResponse) {
    return lastResponse;
  }
  throw lastError || new Error("Request failed after retries");
}

function stamp() {
  return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function getOrCreateSessionId() {
  const existing = sessionStorage.getItem(SESSION_STORAGE_KEY);
  if (existing) {
    return existing;
  }
  const created = crypto.randomUUID();
  sessionStorage.setItem(SESSION_STORAGE_KEY, created);
  return created;
}

function isTraceListingQuestion(question) {
  const text = String(question || "").trim().toLowerCase();
  if (!text) {
    return false;
  }

  return [
    "list trace",
    "show trace",
    "all trace",
    "trace id",
    "traceid",
    "trace ids",
    "traceids",
    "list out",
    "list all trace",
    "list all traces",
    "enumerate trace",
    "print trace",
    "which trace",
    "what trace",
    "get all trace",
    "retrieve trace",
    "find trace",
    "search trace",
    "each trace",
  ].some((keyword) => text.includes(keyword));
}

function normalizeMessageText(input) {
  if (input && typeof input === "object") {
    if (typeof input.explanation === "string") {
      return input.explanation.replace(/\\n/g, "\n").replace(/\\t/g, "\t");
    }
    if (typeof input.answer === "string") {
      return input.answer.replace(/\\n/g, "\n").replace(/\\t/g, "\t");
    }
    return JSON.stringify(input, null, 2);
  }

  if (typeof input !== "string") {
    return String(input ?? "");
  }

  let text = input;
  text = text.replace(/```(?:json)?/gi, "").replace(/```/g, "").trim();
  if (text.includes("\\n") && !text.includes("\n")) {
    text = text.replace(/\\n/g, "\n").replace(/\\t/g, "\t");
  }

  if ((text.startsWith("{") && text.endsWith("}")) || (text.startsWith("[") && text.endsWith("]"))) {
    try {
      const parsed = JSON.parse(text);
      if (parsed && typeof parsed === "object") {
        if (typeof parsed.explanation === "string") {
          return parsed.explanation.replace(/\\n/g, "\n").replace(/\\t/g, "\t");
        }
        if (typeof parsed.answer === "string") {
          return parsed.answer.replace(/\\n/g, "\n").replace(/\\t/g, "\t");
        }
      }
      return JSON.stringify(parsed, null, 2);
    } catch {
      return text;
    }
  }

  return text;
}

function compactMessageForMemory(text, limit = 420) {
  const normalized = normalizeMessageText(text).replace(/\s+/g, " ").trim();
  if (normalized.length <= limit) {
    return normalized;
  }
  return `${normalized.slice(0, limit).trim()}...`;
}

function buildAnalystMemory(logMessages, latestMerged, analystMode, anchors, totalFleetTraces) {
  const recentTurns = logMessages
    .filter((msg) => msg.role === "user" || msg.role === "assistant")
    .slice(-(ANALYST_MEMORY_LIMIT - 1))
    .map((msg) => ({
      role: msg.role,
      text: compactMessageForMemory(msg.text),
    }));

  const contextLines = [];
  if (analystMode === "single_trace") {
    const sessionId = anchors.session_id || latestMerged?.request?.session_id || "";
    const traceId = anchors.xray_trace_id || latestMerged?.request?.trace_id || "";
    const slowestStep = latestMerged?.xray?.slowest_step?.name || "";
    if (sessionId) {
      contextLines.push(`Current session anchor: ${sessionId}.`);
    }
    if (traceId) {
      contextLines.push(`Current trace anchor: ${traceId}.`);
    }
    if (slowestStep) {
      contextLines.push(`Latest slowest X-Ray step: ${slowestStep}.`);
    }
  } else if (latestMerged) {
    const bottleneck = latestMerged?.bottleneck_ranking?.[0]?.component || "n/a";
    const avgLatency = latestMerged?.fleet_metrics?.e2e_ms?.avg || 0;
    contextLines.push(`Current window covers ${totalFleetTraces || 0} traces.`);
    contextLines.push(`Top bottleneck: ${bottleneck}.`);
    contextLines.push(`Average end-to-end latency: ${avgLatency} ms.`);
  }

  const contextEntry = contextLines.length
    ? [{ role: "context", text: contextLines.join(" ") }]
    : [];

  return [...contextEntry, ...recentTurns];
}

function TypingIndicator() {
  return (
    <div className="typing" aria-live="polite" aria-label="Assistant is typing">
      <span />
      <span />
      <span />
    </div>
  );
}

function App({ signOut, user }) {
  const [chatSessionId, setChatSessionId] = useState(() => getOrCreateSessionId());
  const [mode, setMode] = useState("assistant");
  const [prompt, setPrompt] = useState("");
  const [messages, setMessages] = useState([
    {
      id: crypto.randomUUID(),
      role: "system",
      text: "Connected. Ask anything to start chatting.",
      variant: "status",
      timestamp: stamp(),
    },
  ]);
  const [logQuestion, setLogQuestion] = useState("");
  const [logMessages, setLogMessages] = useState([]);
  const [autoAnchors, setAutoAnchors] = useState({
    request_id: "",
    client_request_id: "",
    // Keep the ongoing chat/evaluator session visible even before analyst calls.
    session_id: chatSessionId,
    evaluator_session_id: "",
    xray_trace_id: "",
  });
  const [analystAnchors, setAnalystAnchors] = useState({
    request_id: "",
    client_request_id: "",
    session_id: "",
    evaluator_session_id: "",
    xray_trace_id: "",
  });
  const [latestMerged, setLatestMerged] = useState(null);
  const [busyChat, setBusyChat] = useState(false);
  const [busyLog, setBusyLog] = useState(false);
  const [analystMode, setAnalystMode] = useState("fleet_window");
  const [logLookbackHours, setLogLookbackHours] = useState("1");
  const [fleetPage, setFleetPage] = useState(1);
  const [lastFleetQuestion, setLastFleetQuestion] = useState("");
  const [latestPagination, setLatestPagination] = useState(null);
  const [latestSessionsPagination, setLatestSessionsPagination] = useState(null);
  const [autoPageProgress, setAutoPageProgress] = useState(null);
  const [controlsOpen, setControlsOpen] = useState(true);
  const [copiedId, setCopiedId] = useState("");
  const [profile, setProfile] = useState({ name: user?.username || "User", department: "", role: "" });
  const chatAbortRef = useRef(null);
  const analystAbortRef = useRef(null);
  const messagesViewportRef = useRef(null);
  const shouldStickToBottomRef = useRef(true);

  const effectiveAnchors = useMemo(
    () => ({
      request_id: analystAnchors.request_id || autoAnchors.request_id,
      client_request_id: analystAnchors.client_request_id || autoAnchors.client_request_id,
      session_id: analystAnchors.session_id || autoAnchors.session_id,
      evaluator_session_id: analystAnchors.evaluator_session_id || autoAnchors.evaluator_session_id,
      xray_trace_id: analystAnchors.xray_trace_id || autoAnchors.xray_trace_id,
    }),
    [analystAnchors, autoAnchors],
  );

  const selectedLookbackLabel = useMemo(() => {
    const found = LOOKBACK_OPTIONS.find((opt) => opt.value === logLookbackHours);
    return found?.label || `${logLookbackHours} hours`;
  }, [logLookbackHours]);

  const isBusy = busyChat || busyLog;

  const isFleetMode = analystMode !== "single_trace";
  const totalFleetTraces = latestPagination?.total_traces || latestMerged?.fleet_metrics?.traces_total || 0;
  const analystMemory = useMemo(
    () => buildAnalystMemory(logMessages, latestMerged, analystMode, effectiveAnchors, totalFleetTraces),
    [logMessages, latestMerged, analystMode, effectiveAnchors, totalFleetTraces],
  );
  const analystMetricCards = useMemo(() => {
    if (!latestMerged) {
      return [];
    }

    if (isFleetMode) {
      return [
        { label: "Traces in view", value: String(totalFleetTraces || 0), hint: selectedLookbackLabel },
        { label: "Average latency", value: `${latestMerged?.fleet_metrics?.e2e_ms?.avg || 0} ms`, hint: "End-to-end" },
        { label: "Top bottleneck", value: latestMerged?.bottleneck_ranking?.[0]?.component || "n/a", hint: "Primary delay source" },
      ];
    }

    return [
      { label: "Runtime records", value: String(latestMerged.runtime_records_found || 0), hint: "Logs matched" },
      { label: "Evaluator records", value: String(latestMerged.evaluator_records_found || 0), hint: "Metrics matched" },
      { label: "Slowest X-Ray step", value: latestMerged?.xray?.slowest_step?.name || "n/a", hint: `${latestMerged?.xray?.slowest_step?.duration_ms || 0} ms` },
    ];
  }, [isFleetMode, latestMerged, selectedLookbackLabel, totalFleetTraces]);
  const assistantContextRows = useMemo(
    () => [
      { label: "Chat session ID", value: chatSessionId, copyKey: "chat-session" },
      { label: "Latest session ID", value: autoAnchors.session_id, copyKey: "assistant-session" },
      { label: "Latest trace ID", value: autoAnchors.xray_trace_id, copyKey: "assistant-trace" },
    ].filter((item) => item.value),
    [autoAnchors.session_id, autoAnchors.xray_trace_id, chatSessionId],
  );
  const analystContextRows = useMemo(
    () => [
      { label: "Session ID", value: effectiveAnchors.session_id, copyKey: "analyst-session" },
      { label: "X-Ray trace ID", value: effectiveAnchors.xray_trace_id, copyKey: "analyst-trace" },
      { label: "Request ID", value: effectiveAnchors.request_id, copyKey: "analyst-request" },
      { label: "Client request ID", value: effectiveAnchors.client_request_id, copyKey: "analyst-client-request" },
      { label: "Evaluator session ID", value: effectiveAnchors.evaluator_session_id, copyKey: "analyst-evaluator-session" },
    ].filter((item) => item.value),
    [effectiveAnchors],
  );

  useEffect(() => {
    async function loadProfile() {
      try {
        const attrs = await fetchUserAttributes();
        setProfile({
          name: attrs.name || user?.username || "User",
          department: attrs["custom:department"] || "",
          role: attrs["custom:role"] || "",
        });
      } catch {
        setProfile({ name: user?.username || "User", department: "", role: "" });
      }
    }
    loadProfile();
  }, [user]);

  function copyToClipboard(value, key) {
    if (!value) {
      return;
    }
    navigator.clipboard.writeText(String(value)).then(() => {
      setCopiedId(key);
      window.setTimeout(() => setCopiedId(""), 1200);
    });
  }

  function makeMessage(role, text, variant = "", extra = {}) {
    return {
      id: crypto.randomUUID(),
      role,
      text: normalizeMessageText(text),
      variant,
      timestamp: stamp(),
      ...extra,
    };
  }

  function handleComposerKeyDown(event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      if (mode === "assistant") {
        sendPrompt(event);
      } else {
        sendLogQuestion(event);
      }
    }
  }

  function resetToNewSession() {
    const nextSessionId = crypto.randomUUID();
    sessionStorage.setItem(SESSION_STORAGE_KEY, nextSessionId);

    if (chatAbortRef.current) {
      chatAbortRef.current.abort();
      chatAbortRef.current = null;
    }
    if (analystAbortRef.current) {
      analystAbortRef.current.abort();
      analystAbortRef.current = null;
    }

    setBusyChat(false);
    setBusyLog(false);
    setChatSessionId(nextSessionId);
    setPrompt("");
    setLogQuestion("");
    setMessages([
      {
        id: crypto.randomUUID(),
        role: "system",
        text: "Started a new session. Ask anything to continue.",
        variant: "status",
        timestamp: stamp(),
      },
    ]);
    setLogMessages([]);
    setLatestMerged(null);
    setLastFleetQuestion("");
    setFleetPage(1);
    setLatestPagination(null);
    setLatestSessionsPagination(null);
    setAutoPageProgress(null);
    setAnalystAnchors({
      request_id: "",
      client_request_id: "",
      session_id: "",
      evaluator_session_id: "",
      xray_trace_id: "",
    });
    setAutoAnchors({
      request_id: "",
      client_request_id: "",
      session_id: nextSessionId,
      evaluator_session_id: "",
      xray_trace_id: "",
    });
  }

  function stopCurrentGeneration() {
    if (busyChat && chatAbortRef.current) {
      chatAbortRef.current.abort();
      chatAbortRef.current = null;
      setBusyChat(false);
      setMessages((prev) => [...prev, makeMessage("system", "Response stopped.", "status")]);
    }
    if (busyLog && analystAbortRef.current) {
      analystAbortRef.current.abort();
      analystAbortRef.current = null;
      setBusyLog(false);
      setAutoPageProgress(null);
      setLogMessages((prev) => [...prev, makeMessage("system", "Analysis stopped.", "metric")]);
    }
  }

  async function sendPrompt(event) {
    event.preventDefault();
    const trimmed = prompt.trim();
    if (!trimmed || isBusy) {
      return;
    }

    setPrompt("");
    setBusyChat(true);
    setMessages((prev) => [
      ...prev,
      makeMessage("user", trimmed),
    ]);

    try {
      const controller = new AbortController();
      chatAbortRef.current = controller;
      const session = await fetchAuthSession();
      const jwtToken = session.tokens?.idToken?.toString() || "";

      const response = await fetch(`${API_BASE_URL}/chat`, {
        method: "POST",
        signal: controller.signal,
        headers: {
          "Content-Type": "application/json",
          Authorization: jwtToken ? `Bearer ${jwtToken}` : "",
          "x-session-id": chatSessionId,
        },
        body: JSON.stringify({
          prompt: trimmed,
          session_id: chatSessionId,
          user_context: {
            user_id: user?.username || "anonymous",
            user_name: profile.name || user?.username || "anonymous",
            department: profile.department || "",
            user_role: profile.role || "",
          },
        }),
      });

      const data = await response.json().catch(() => ({}));
      const answer = response.ok
        ? data.answer || "No answer returned."
        : data.error || data.message || `Request failed (${response.status})`;
      const trace = response.ok ? data.trace || {} : {};

      setMessages((prev) => {
        const next = [...prev, makeMessage("assistant", answer)];

        // Trace metadata is captured but not displayed in chat to keep UI clean

        return next;
      });

      if (response.ok && trace) {
        setAutoAnchors((prev) => ({
          request_id: trace.request_id || prev.request_id,
          client_request_id: trace.client_request_id || prev.client_request_id,
          session_id: trace.session_id || prev.session_id || chatSessionId,
          evaluator_session_id: trace.evaluator_session_id || prev.evaluator_session_id,
          xray_trace_id: trace.xray_trace_id || prev.xray_trace_id,
        }));
      }
    } catch (error) {
      if (error?.name === "AbortError") {
        return;
      }
      setMessages((prev) => [...prev, makeMessage("assistant", `Unable to fetch response. ${error?.message || ""}`.trim())]);
    } finally {
      chatAbortRef.current = null;
      setBusyChat(false);
    }
  }

  async function sendLogQuestion(event) {
    event.preventDefault();
    const trimmed = logQuestion.trim();
    if (!trimmed || isBusy) {
      return;
    }

    if (analystMode === "single_trace" && !effectiveAnchors.xray_trace_id) {
      setLogMessages((prev) => [
        ...prev,
        makeMessage("system", "No trace ID available yet. Send one assistant prompt first to capture a trace ID.", "metric"),
      ]);
      return;
    }

    if (isFleetMode) {
      setLastFleetQuestion(trimmed);
      setFleetPage(1);
    }

    await requestLogAnalysis(trimmed, {
      page: 1,
      addUserMessage: true,
      autoPaginate: isFleetMode,
    });
  }

  async function requestLogAnalysis(questionText, options = {}) {
    const {
      page = 1,
      addUserMessage = false,
      autoPaginate = false,
    } = options;

    if (!questionText || isBusy) {
      return;
    }

    setBusyLog(true);
    if (addUserMessage) {
      setLogQuestion("");
      setLogMessages((prev) => [
        ...prev,
        makeMessage("user", questionText),
      ]);
    }

    try {
      const controller = new AbortController();
      analystAbortRef.current = controller;
      const session = await fetchAuthSession();
      const jwtToken = session.tokens?.idToken?.toString() || "";
      setAutoPageProgress(null);

      async function fetchAnalysisPage(targetPage) {
        const response = await postWithRetry(`${API_BASE_URL}/session-insights`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: jwtToken ? `Bearer ${jwtToken}` : "",
            "x-session-id": chatSessionId,
          },
          body: JSON.stringify({
            question: questionText,
            analysis_mode: analystMode,
            analyst_memory: analystMemory,
            request_id: analystMode === "single_trace" ? effectiveAnchors.request_id : "",
            client_request_id: analystMode === "single_trace" ? effectiveAnchors.client_request_id : "",
            session_id: analystMode === "single_trace" ? effectiveAnchors.session_id : "",
            evaluator_session_id: analystMode === "single_trace" ? effectiveAnchors.evaluator_session_id : "",
            xray_trace_id: analystMode === "single_trace" ? effectiveAnchors.xray_trace_id : "",
            page: targetPage,
            page_size: DEFAULT_FLEET_PAGE_SIZE,
            lookback_hours:
              analystMode === "single_trace"
                ? 48
                : logLookbackHours === "overall"
                  ? "overall"
                  : Number(logLookbackHours),
            lookback_mode:
              analystMode === "single_trace"
                ? "window"
                : logLookbackHours === "overall"
                  ? "overall"
                  : "window",
            user_context: {
              user_id: user?.username || "anonymous",
              user_name: profile.name || user?.username || "anonymous",
              department: profile.department || "",
              user_role: profile.role || "",
            },
          }),
        }, ANALYSIS_MAX_RETRIES, controller.signal);

        const data = await response.json().catch(() => ({}));
        const answer = response.ok
          ? data.answer || "No analysis returned."
          : data.error || data.message || `Request failed (${response.status})`;

        return { response, data, answer };
      }

      const firstResult = await fetchAnalysisPage(page);
      const firstPagination = firstResult.data?.pagination || null;
      const firstPrefix = isFleetMode && Number(firstPagination?.page || page) > 1 && Number(firstPagination?.total_pages || 1) > 1
        ? `Page ${firstPagination.page} of ${firstPagination.total_pages}`
        : "";

      setLogMessages((prev) => [
        ...prev,
        makeMessage("assistant", firstPrefix ? `${firstPrefix}\n\n${firstResult.answer}` : firstResult.answer),
      ]);

      if (firstResult.response.ok && analystMode !== "single_trace") {
        setFleetPage(Number(firstPagination?.page || page || 1));
        setLatestPagination(firstPagination);
        setLatestSessionsPagination(firstResult.data?.sessions_pagination || null);
      } else {
        setLatestPagination(null);
        setLatestSessionsPagination(null);
      }

      if (firstResult.response.ok && firstResult.data?.anchors && analystMode === "single_trace") {
        setAnalystAnchors((prev) => ({
          request_id: firstResult.data.anchors.request_id || prev.request_id,
          client_request_id: firstResult.data.anchors.client_request_id || prev.client_request_id,
          session_id: firstResult.data.anchors.session_id || prev.session_id,
          evaluator_session_id: firstResult.data.anchors.evaluator_session_id || prev.evaluator_session_id,
          xray_trace_id: firstResult.data.anchors.xray_trace_id || prev.xray_trace_id,
        }));
      }
      setLatestMerged(firstResult.response.ok ? firstResult.data?.merged || null : null);

      const shouldAutoLoadRemainingPages =
        firstResult.response.ok
        && analystMode !== "single_trace"
        && autoPaginate
        && Number(firstPagination?.total_pages || 1) > 1;

      if (shouldAutoLoadRemainingPages) {
        const totalPages = Number(firstPagination?.total_pages || 1);
        let loadedPages = Number(firstPagination?.page || 1);
        setAutoPageProgress({ loadedPages, totalPages });

        for (let nextPage = loadedPages + 1; nextPage <= totalPages; nextPage += 1) {
          const nextResult = await fetchAnalysisPage(nextPage);

          if (!nextResult.response.ok) {
            setLogMessages((prev) => [
              ...prev,
              makeMessage(
                "system",
                `Stopped auto-loading after page ${loadedPages}. Page ${nextPage} failed with: ${nextResult.answer}`,
                "metric",
              ),
            ]);
            break;
          }

          loadedPages = Number(nextResult.data?.pagination?.page || nextPage);
          setFleetPage(loadedPages);
          setLatestPagination(nextResult.data?.pagination || null);
          setLatestSessionsPagination(nextResult.data?.sessions_pagination || null);
          setLatestMerged(nextResult.data?.merged || null);
          setAutoPageProgress({ loadedPages, totalPages });
          setLogMessages((prev) => [
            ...prev,
            makeMessage("assistant", `Page ${loadedPages} of ${totalPages}\n\n${nextResult.answer}`),
          ]);
        }
      }
    } catch (error) {
      if (error?.name === "AbortError") {
        return;
      }
      setAutoPageProgress(null);
      setLatestPagination(null);
      setLogMessages((prev) => [...prev, makeMessage("assistant", `Unable to fetch analysis. ${error?.message || ""}`.trim())]);
    } finally {
      analystAbortRef.current = null;
      setBusyLog(false);
    }
  }

  async function goToFleetPage(nextPage) {
    if (!lastFleetQuestion || !isFleetMode || isBusy) {
      return;
    }
    if (nextPage < 1) {
      return;
    }
    if (latestPagination?.total_pages && nextPage > latestPagination.total_pages) {
      return;
    }
    await requestLogAnalysis(lastFleetQuestion, { page: nextPage, addUserMessage: false, autoPaginate: false });
  }

  function handleMessagesScroll(event) {
    const node = event.currentTarget;
    const distanceFromBottom = node.scrollHeight - node.scrollTop - node.clientHeight;
    shouldStickToBottomRef.current = distanceFromBottom < 100;
  }

  useEffect(() => {
    const node = messagesViewportRef.current;
    if (!node || !shouldStickToBottomRef.current) {
      return;
    }

    const raf = window.requestAnimationFrame(() => {
      node.scrollTop = node.scrollHeight;
    });
    return () => window.cancelAnimationFrame(raf);
  }, [messages, logMessages, busyChat, busyLog, mode]);

  function renderMessages(items, loading, viewportRef, onScroll) {
    return (
      <div className="messages" ref={viewportRef} onScroll={onScroll}>
        {items.map((msg) => (
          <article key={msg.id} className={`message ${msg.role} ${msg.variant || ""}`}>
            <div className="bubble-wrap">
              <div className="bubble-header">
                <span className="who">{msg.role === "assistant" ? "Assistant" : msg.role === "user" ? "You" : "System"}</span>
                <span className="time">{msg.timestamp}</span>
              </div>
              <div className="bubble">{msg.text}</div>
              {msg.role === "assistant" && (
                <button
                  className="copy-btn"
                  type="button"
                  onClick={() => copyToClipboard(msg.text, msg.id)}
                  title="Copy message"
                >
                  {copiedId === msg.id ? "Copied" : "Copy"}
                </button>
              )}
            </div>
          </article>
        ))}
        {loading && (
          <article className="message assistant typing-row">
            <div className="bubble-wrap">
              <div className="bubble-header">
                <span className="who">Assistant</span>
                <span className="time">{stamp()}</span>
              </div>
              <div className="bubble">
                <TypingIndicator />
              </div>
            </div>
          </article>
        )}
      </div>
    );
  }

  const isAssistantMode = mode === "assistant";
  const activeMessages = isAssistantMode ? messages : logMessages;
  const activeBusy = isAssistantMode ? busyChat : busyLog;
  const activeQuestion = isAssistantMode ? prompt : logQuestion;
  const activeCharCount = isAssistantMode ? prompt.length : logQuestion.length;

  return (
    <main className="sensei-shell">
      <header className="sensei-topbar">
        <div className="sensei-brand">
          <strong>PHILIPS</strong>
          <span>SENSEI</span>
        </div>

        <nav className="sensei-nav" role="tablist" aria-label="Console mode">
          <button
            type="button"
            className={`nav-tab ${isAssistantMode ? "active" : ""}`}
            onClick={() => setMode("assistant")}
          >
            Assistant chat
          </button>
          <button
            type="button"
            className={`nav-tab ${!isAssistantMode ? "active" : ""}`}
            onClick={() => setMode("session")}
          >
            Analyser chat
          </button>
        </nav>

        <div className="sensei-user">
          <span>{(profile.name || user?.username || "JD").slice(0, 2).toUpperCase()}</span>
          <button className="new-session" onClick={resetToNewSession} type="button">New session</button>
          <button className="signout" onClick={signOut} type="button">Sign out</button>
        </div>
      </header>

      <section className={`chat-shell ${isAssistantMode ? "assistant-only" : ""} ${!isAssistantMode && !controlsOpen ? "menu-collapsed" : ""}`}>
        {!isAssistantMode && (
          <>
            <button
              type="button"
              className="menu-toggle"
              onClick={() => setControlsOpen((prev) => !prev)}
              aria-expanded={controlsOpen}
            >
              {controlsOpen ? "Hide menu" : "Show menu"}
            </button>

            <aside className={`left-menu ${controlsOpen ? "open" : "collapsed"}`}>
              <div className="left-menu-scroll">
              <div className="analysis-toolbar">
                <label>
                  <span>Mode</span>
                  <select value={analystMode} onChange={(e) => setAnalystMode(e.target.value)}>
                    <option value="fleet_window">Fleet window</option>
                    <option value="single_trace">Single trace</option>
                  </select>
                </label>
                <label>
                  <span>Time</span>
                  <select
                    value={logLookbackHours}
                    onChange={(e) => setLogLookbackHours(e.target.value || "1")}
                    disabled={analystMode === "single_trace"}
                  >
                    {LOOKBACK_OPTIONS.map((opt) => (
                      <option key={opt.value} value={opt.value}>{opt.label}</option>
                    ))}
                  </select>
                </label>
                {analystMode === "single_trace" && (
                  <label className="trace-id-field">
                    <span>X-Ray Trace ID</span>
                    <input
                      value={analystAnchors.xray_trace_id || ""}
                      onChange={(e) =>
                        setAnalystAnchors((prev) => ({
                          ...prev,
                          xray_trace_id: e.target.value.trim(),
                        }))
                      }
                      placeholder={autoAnchors.xray_trace_id || "send a chat message to capture trace ID"}
                    />
                  </label>
                )}
              </div>

              <div className="menu-suggestions">
                <p>Suggestions</p>
                {ANALYSER_SUGGESTIONS.map((suggestion, index) => (
                  <button
                    key={suggestion}
                    type="button"
                    className="suggestion-chip"
                    onClick={() => setLogQuestion(suggestion)}
                    disabled={isBusy}
                  >
                    <span className="suggestion-index">0{index + 1}</span>
                    <span className="suggestion-text">{suggestion}</span>
                  </button>
                ))}
              </div>
            </div>
            </aside>
          </>
        )}

        <div className="chat-center">
          <div className="chat-feed">
            {renderMessages(activeMessages, activeBusy, messagesViewportRef, handleMessagesScroll)}
          </div>

          <form className="floating-composer" onSubmit={isAssistantMode ? sendPrompt : sendLogQuestion}>
            <textarea
              value={activeQuestion}
              onChange={(e) => (isAssistantMode ? setPrompt(e.target.value) : setLogQuestion(e.target.value))}
              onKeyDown={handleComposerKeyDown}
              placeholder="Ask anything"
              maxLength={4000}
              disabled={isBusy}
            />
            <div className="composer-row">
              <p className="char-count">{activeCharCount}/4000</p>
              <div className="composer-actions">
                {isBusy && (
                  <button className="stop" type="button" onClick={stopCurrentGeneration}>
                    Stop
                  </button>
                )}
                <button className="send" type="submit" disabled={isBusy} title="Send" aria-label="Send">
                  <span className="plane" aria-hidden="true">&#10148;</span>
                </button>
              </div>
            </div>
          </form>
        </div>
      </section>
    </main>
  );
}

export default App;
