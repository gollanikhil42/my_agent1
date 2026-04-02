import { useEffect, useMemo, useRef, useState } from "react";
import { fetchAuthSession, fetchUserAttributes } from "aws-amplify/auth";
import { AnalyzerSetup } from "./components/AnalyzerSetup/AnalyzerSetup";
import { Topbar } from "./components/Topbar/Topbar";
import { HelpPanel } from "./components/HelpPanel/HelpPanel";
import { MessageList } from "./components/MessageList/MessageList";
import { Composer } from "./components/Composer/Composer";

const SESSION_STORAGE_KEY = "session_id";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL;
const ANALYSIS_MAX_RETRIES = 2;
const ANALYST_MEMORY_LIMIT = 6;
const RETRYABLE_STATUS = new Set([429, 502, 503]);
const DEFAULT_FLEET_PAGE_SIZE = 20;
const ANALYZER_HELP_DISMISSED_KEY = "analyzer_help_dismissed";

const ANALYZER_HELP_SECTIONS = [
  {
    title: "Fleet Mode",
    text: "All analysis uses fleet mode—we analyze patterns, summaries, and issues across many sessions in your selected timeframe.",
  },
  {
    title: "Timeframes",
    text: "You choose an initial timeframe (30m, 1h, 4h, 1d, or overall). You can change it anytime mid-conversation just by asking (e.g., 'switch to 5 days' or 'analyze last 3 hours').",
  },
  {
    title: "What to ask",
    text: "Ask about patterns, affected users, slow sessions, error distributions, bottlenecks, and what can be improved. The analyzer uses logs, evaluator data, and X-Ray traces to answer. Ask for suggestions when you need them—the model will provide examples.",
  },
];

const FLEET_MODE_SUGGESTIONS = [
  "Summarize the top performance issues in this window",
  "Which users experienced the most errors?",
  "What are the main bottlenecks affecting latency?",
  "List the slowest sessions and their characteristics",
  "How many traces had errors vs. success?",
];

const ENABLE_SUGGESTIONS_BUTTON = false;

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

function formatResponseForReadability(text) {
  if (!text || typeof text !== "string") {
    return text;
  }

  // Preserve the original text structure but improve spacing
  let formatted = text;

  // Add extra space after periods that end sentences followed by a capital letter
  formatted = formatted.replace(/(\.\s+)([A-Z])/g, "$1\n\n$2");

  // Ensure consistent line spacing for bullet points and numbered lists
  formatted = formatted.replace(/\n(-|\d+\.)\s+/g, "\n$1 ");

  // Add spacing after colons that introduce content (if followed by bullet points)
  formatted = formatted.replace(/:\n(-|\d+\.)/g, ":\n$1");

  // Ensure double line breaks between paragraphs (collapse excessive spacing)
  formatted = formatted.replace(/\n{3,}/g, "\n\n");

  return formatted.trim();
}

function buildAnalystMemory(logMessages, latestMerged, anchors, totalFleetTraces) {
  const recentTurns = logMessages
    .filter((msg) => msg.role === "user" || msg.role === "assistant")
    .slice(-(ANALYST_MEMORY_LIMIT - 1))
    .map((msg) => ({
      role: msg.role,
      text: compactMessageForMemory(msg.text),
    }));

  const contextLines = [];
  const hasTraceContext = Boolean(anchors.xray_trace_id || latestMerged?.request?.trace_id || latestMerged?.xray);
  if (hasTraceContext) {
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
  }

  if (!hasTraceContext && latestMerged) {
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

function makeAnalystWelcomeMessage() {
  return {
    id: crypto.randomUUID(),
    role: "system",
    text: "Welcome to the analyzer! I'm here to help you analyze your trace data. You can ask questions about your fleet, and I'll analyze a default 30-day window. Want to change the timeframe? Just say 'switch to 7 days' or 'show last week'.",
    variant: "metric",
    timestamp: stamp(),
  };
}

function makeAnalystTimeframeQuestion() {
  return {
    id: crypto.randomUUID(),
    role: "system",
    text: "Which timeframe? Examples: 30m, 1h, 4h, 1d, overall. Just say the timeframe. You can change it anytime.",
    variant: "metric",
    timestamp: stamp(),
  };
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
  const [logMessages, setLogMessages] = useState([makeAnalystWelcomeMessage()]);
  const [analyzerTimeframe, setAnalyzerTimeframe] = useState("30d"); // Default to 30 days for general conversation
  const [analyzerSetupStep, setAnalyzerSetupStep] = useState("complete");
  const [analyzerSetupComplete, setAnalyzerSetupComplete] = useState(true);
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
  const [fleetPage, setFleetPage] = useState(1);
  const [lastFleetQuestion, setLastFleetQuestion] = useState("");
  const [latestPagination, setLatestPagination] = useState(null);
  const [latestSessionsPagination, setLatestSessionsPagination] = useState(null);
  const [autoPageProgress, setAutoPageProgress] = useState(null);
  const [copiedId, setCopiedId] = useState("");
  const [helpOpen, setHelpOpen] = useState(false);
  const [suggestionsOpen, setSuggestionsOpen] = useState(false);
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

  const totalFleetTraces = latestPagination?.total_traces || latestMerged?.fleet_metrics?.traces_total || 0;
  const analystMemory = useMemo(
    () => buildAnalystMemory(logMessages, latestMerged, effectiveAnchors, totalFleetTraces),
    [logMessages, latestMerged, effectiveAnchors, totalFleetTraces],
  );
  const analystMetricCards = useMemo(() => {
    if (!latestMerged) {
      return [];
    }

    const hasTraceContext = Boolean(latestMerged?.xray || latestMerged?.request?.trace_id || effectiveAnchors.xray_trace_id);
    if (!hasTraceContext) {
      return [
        { label: "Traces in view", value: String(totalFleetTraces || 0), hint: "Auto-selected timeframe" },
        { label: "Average latency", value: `${latestMerged?.fleet_metrics?.e2e_ms?.avg || 0} ms`, hint: "End-to-end" },
        { label: "Top bottleneck", value: latestMerged?.bottleneck_ranking?.[0]?.component || "n/a", hint: "Primary delay source" },
      ];
    }

    return [
      { label: "Runtime records", value: String(latestMerged.runtime_records_found || 0), hint: "Logs matched" },
      { label: "Evaluator records", value: String(latestMerged.evaluator_records_found || 0), hint: "Metrics matched" },
      { label: "Slowest X-Ray step", value: latestMerged?.xray?.slowest_step?.name || "n/a", hint: `${latestMerged?.xray?.slowest_step?.duration_ms || 0} ms` },
    ];
  }, [effectiveAnchors.xray_trace_id, latestMerged, totalFleetTraces]);
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

  useEffect(() => {
    if (mode !== "session") {
      return;
    }
    if (!sessionStorage.getItem(ANALYZER_HELP_DISMISSED_KEY)) {
      setHelpOpen(true);
    }
  }, [mode]);

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
    const normalized = normalizeMessageText(text);
    const formatted = role === "assistant" ? formatResponseForReadability(normalized) : normalized;
    return {
      id: crypto.randomUUID(),
      role,
      text: formatted,
      variant,
      timestamp: stamp(),
      ...extra,
    };
  }

  function handleComposerKeyDown(event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      const form = event.target.closest('form');
      if (form) {
        form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
      }
    }
  }

  function applySuggestion(suggestion) {
    if (isAssistantMode) {
      setPrompt(suggestion);
    } else {
      setLogQuestion(suggestion);
    }
    setSuggestionsOpen(false);
  }

  // Fleet-only: directly select timeframe (no mode selection needed)
  function handleSelectAnalyzerTimeframe(timeframe) {
    setAnalyzerTimeframe(timeframe);
    setAnalyzerSetupComplete(true);
    setLogQuestion("");
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
    setLogMessages([makeAnalystWelcomeMessage(), makeAnalystTimeframeQuestion()]);
    setAnalyzerTimeframe(null);
    setAnalyzerSetupStep("awaiting_timeframe");
    setAnalyzerSetupComplete(false);
    setLatestMerged(null);
    setLastFleetQuestion("");
    setFleetPage(1);
    setLatestPagination(null);
    setLatestSessionsPagination(null);
    setAutoPageProgress(null);
    setAnalyzerSetupComplete(false);
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
    if (mode === "assistant" && busyChat && chatAbortRef.current) {
      chatAbortRef.current.abort();
      chatAbortRef.current = null;
      setBusyChat(false);
      setMessages((prev) => [...prev, makeMessage("system", "Response stopped.", "status")]);
    }
    if (mode !== "assistant" && busyLog && analystAbortRef.current) {
      analystAbortRef.current.abort();
      analystAbortRef.current = null;
      setBusyLog(false);
      setAutoPageProgress(null);
      setLogMessages((prev) => [...prev, makeMessage("system", "Analysis stopped.", "metric")]);
    }
  }

  async function sendPrompt(event) {
    if (event) {
      event.preventDefault();
    }
    const trimmed = prompt.trim();
    if (!trimmed || busyChat) {
      console.log('[sendPrompt] Early return:', { emptyPrompt: !trimmed, busy: busyChat });
      return;
    }
    console.log('[sendPrompt] Sending:', trimmed.substring(0, 50));

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

  // Helper functions for analyzer setup (fleet-only)
  function parseTimeframe(text) {
    const trimmed = text.toLowerCase().trim();
    // Handle standalone "overall" - limited to 30 days max
    if (trimmed === "overall") return "30d"; // Cap overall to 30 days
    
    // Enhanced regex to handle multiple formats: "1min", "7minutes", "6hrs", "8hours", "27days", "to 1h", "set timeframe to 30 days", etc.
    const patterns = [
      // Match: "set timeframe to 30 days", "change to 30 days", "switch to 7 days", etc.
      /(?:set\s+timeframe\s+)?(?:to|for|in|switch\s+to|change\s+to)?\s*(\d+)\s*(minute|minutes|min|m|hour|hours|hr|h|day|days|d|week|weeks|w)\b/i,
      /^(\d+)\s*(minute|minutes|min|m|hour|hours|hr|h|day|days|d|week|weeks|w)$/, // Direct format
    ];
    
    let match = null;
    for (const pattern of patterns) {
      match = trimmed.match(pattern);
      if (match) break;
    }
    
    if (!match) return null;
    
    const num = parseInt(match[1], 10);
    const unit = match[2].toLowerCase();
    
    // Normalize unit to single letter and check 30-day limit
    let normalizedUnit = '';
    let daysEquivalent = 0;
    
    if (['minute', 'minutes', 'min', 'm'].includes(unit)) {
      daysEquivalent = num / (24 * 60);
      if (daysEquivalent > 30) return null; // Exceeds 30 days
      normalizedUnit = 'm';
    } else if (['hour', 'hours', 'hr', 'h'].includes(unit)) {
      daysEquivalent = num / 24;
      if (daysEquivalent > 30) return null; // Exceeds 30 days
      normalizedUnit = 'h';
    } else if (['day', 'days', 'd'].includes(unit)) {
      daysEquivalent = num;
      if (daysEquivalent > 30) return null; // Exceeds 30 days
      normalizedUnit = 'd';
    } else if (['week', 'weeks', 'w'].includes(unit)) {
      daysEquivalent = num * 7;
      if (daysEquivalent > 30) return null; // Exceeds 30 days
      normalizedUnit = 'w';
    } else {
      return null; // Unknown unit
    }
    
    return `${num}${normalizedUnit}`;
  }

  function timeframeToHours(timeframeStr) {
    // Convert "1h", "2d", "30m" to numeric hours (max 30 days = 720 hours)
    // Always ensure we have a timeframe - fallback to 30d if missing
    const safeTimeframe = timeframeStr || "30d";
    if (safeTimeframe === "overall") return 720; // 30 days max
    
    const match = safeTimeframe.match(/^(\d+)([mhdw])$/);
    if (!match) return 720; // Fallback to 30 days if pattern doesn't match
    
    const [, num, unit] = match;
    const amount = parseInt(num, 10);
    const hours = (() => {
      switch (unit) {
        case "m": return Math.max(1, amount / 60);
        case "h": return amount;
        case "d": return amount * 24;
        case "w": return amount * 24 * 7;
        default: return 720;
      }
    })();
    
    // Enforce 30-day max (720 hours)
    return Math.min(hours, 720);
  }

  function hoursToTimeframe(hours) {
    // Convert numeric hours back to "Xd", "Xh", "Xm" format
    if (!hours || hours <= 0) return "1d";
    if (hours >= 720) return "30d"; // max 30 days
    if (hours >= 24) {
      const days = Math.round(hours / 24);
      return `${days}d`;
    }
    if (hours >= 1) {
      const h = Math.round(hours);
      return `${h}h`;
    }
    const minutes = Math.round(hours * 60);
    return `${minutes}m`;
  }

  function handleChangeAnalyzerMode() {
    // Reset to timeframe selection (no mode selection anymore)
    setAnalyzerSetupStep("awaiting_timeframe");
    setAnalyzerSetupComplete(false);
    setAnalyzerTimeframe(null);
    setLogMessages((prev) => [...prev, makeAnalystTimeframeQuestion()]);
  }

  async function sendLogQuestion(event) {
    if (event) {
      event.preventDefault();
    }
    const trimmed = logQuestion.trim();
    if (!trimmed || busyLog) {
      console.log('[sendLogQuestion] Early return:', { emptyQuestion: !trimmed, busy: busyLog });
      return;
    }
    console.log('[sendLogQuestion] Sending:', trimmed.substring(0, 50));

    // SETUP STATE MACHINE — Fleet mode only: ask for timeframe, but allow "General Mode" skip
    if (analyzerSetupStep === "awaiting_timeframe") {
      const trimmedLower = trimmed.toLowerCase();
      // Allow user to skip timeframe with "general" or "skip" command
      if (trimmedLower === "general" || trimmedLower === "skip" || trimmedLower === "general mode") {
        setAnalyzerTimeframe("30d"); // Default to 30 days for general mode
        setLogMessages((prev) => [
          ...prev,
          makeMessage("user", trimmed),
          makeMessage("system", "General mode enabled! Using default 30-day window. You can change the timeframe anytime by saying 'switch to 7 days', 'show last week', etc.", "success"),
        ]);
        setAnalyzerSetupStep("complete");
        setAnalyzerSetupComplete(true);
        setLogQuestion("");
        return;
      }

      const detected = parseTimeframe(trimmed);
      // Check if timeframe exceeded 30-day limit
      if (/^\d+\s*(minute|minutes|min|m|hour|hours|hr|h|day|days|d|week|weeks|w)\b/i.test(trimmed) && !detected) {
        // Looks like a timeframe attempt but exceeded 30-day limit
        setLogMessages((prev) => [
          ...prev,
          makeMessage("system", "We can only analyze data from the last 30 days. Please choose a timeframe within 1-30 days (e.g., 1min, 7h, 15d, 4w) or 'overall' for available data.", "error"),
        ]);
        setLogQuestion("");
        return;
      }
      if (!detected) {
        if (/^\d+\s*(m|min|h|hr|d|days?|w|weeks?)$/i.test(trimmed)) {
          // Looks like a timeframe but exceeds 30-day limit
          setLogMessages((prev) => [
            ...prev,
            makeMessage("system", "We can only analyze data from the last 30 days. Please choose a timeframe within that window (e.g., 1d, 7d, 30d, or overall for available data).", "error"),
          ]);
        } else {
          // Anything else is treated as a general question - auto-enter general mode with 30d default
          setAnalyzerTimeframe("30d");
          setLogMessages((prev) => [
            ...prev,
            makeMessage("user", trimmed),
            makeMessage("system", "Got it! Analyzing with default 30-day window. You can change the timeframe by saying 'switch to 7 days', 'show last week', etc.", "success"),
          ]);
          setAnalyzerSetupStep("complete");
          setAnalyzerSetupComplete(true);
          setLogQuestion("");
          return;
        }
        setLogQuestion("");
        return;
      }
      // Valid timeframe detected
      setAnalyzerTimeframe(detected);
      setLogMessages((prev) => [
        ...prev,
        makeMessage("user", trimmed),
      ]);
      setAnalyzerSetupStep("complete");
      setAnalyzerSetupComplete(true);
      setLogMessages((prev) => [
        ...prev,
        makeMessage("system", `Ready! Analyzing fleet data for the last ${detected}. Ask any questions.`, "success"),
      ]);
      setLogQuestion("");
      return;
    }

    // NORMAL QUESTION FLOW (if setup is complete)
    // Don't try to detect timeframe changes locally - let backend LLM handle it
    // Backend will return timeframe_change if user asked to change it
    
    setLastFleetQuestion(trimmed);
    setFleetPage(1);

    await requestLogAnalysis(trimmed, {
      page: 1,
      addUserMessage: true,
      autoPaginate: true,
    });
  }

  async function requestLogAnalysis(questionText, options = {}) {
    const {
      page = 1,
      addUserMessage = false,
      autoPaginate = false,
    } = options;

    if (!questionText || busyLog) {
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
      setAutoPageProgress(null);

      async function fetchAnalysisPage(targetPage, forceTokenRefresh = false) {
        const session = await fetchAuthSession({ forceRefresh: forceTokenRefresh });
        const jwtToken = session.tokens?.idToken?.toString() || "";
        const response = await postWithRetry(`${API_BASE_URL}/session-insights`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: jwtToken ? `Bearer ${jwtToken}` : "",
            "x-session-id": chatSessionId,
          },
          body: JSON.stringify({
            question: questionText,
            analysis_mode: "fleet_window",
            lookback_hours: timeframeToHours(analyzerTimeframe),
            analyst_memory: analystMemory,
            auto_paginate_requested: autoPaginate,
            request_id: effectiveAnchors.request_id,
            client_request_id: effectiveAnchors.client_request_id,
            session_id: effectiveAnchors.session_id,
            evaluator_session_id: effectiveAnchors.evaluator_session_id,
            xray_trace_id: effectiveAnchors.xray_trace_id,
            page: targetPage,
            page_size: DEFAULT_FLEET_PAGE_SIZE,
            user_context: {
              user_id: user?.username || "anonymous",
              user_name: profile.name || user?.username || "anonymous",
              department: profile.department || "",
              user_role: profile.role || "",
            },
          }),
        }, ANALYSIS_MAX_RETRIES, controller.signal);

        // If we get a 401, force-refresh the token and retry once (handles transient JWT expiry).
        if (response.status === 401 && !forceTokenRefresh) {
          return fetchAnalysisPage(targetPage, true);
        }

        const data = await response.json().catch(() => ({}));
        const answer = response.ok
          ? data.answer || "No analysis returned."
          : data.error || data.message || `Request failed (${response.status})`;

        return { response, data, answer };
      }

      const firstResult = await fetchAnalysisPage(page);
      const firstPagination = firstResult.data?.pagination || null;
      const firstSessionsPagination = firstResult.data?.sessions_pagination || null;
      const firstMergedSessionsPagination = firstResult.data?.merged?.sessions_pagination_context || null;
      const firstMergedPagination = firstResult.data?.merged?.pagination_context || null;
      const firstPageInfo = firstSessionsPagination || firstPagination || firstMergedSessionsPagination || firstMergedPagination;
      const autoPaginateRecommended = Boolean(firstResult.data?.auto_paginate_recommended);
      const firstPrefix = Number(firstPageInfo?.page || page) > 1 && Number(firstPageInfo?.total_pages || 1) > 1
        ? `Page ${firstPageInfo.page} of ${firstPageInfo.total_pages}`
        : "";

      // ALWAYS sync timeframe from backend response (source of truth for current window)
      let currentTimeframe = analyzerTimeframe;
      if (firstResult.response.ok && firstResult.data?.detected_lookback_hours !== undefined) {
        const detectedHours = firstResult.data.detected_lookback_hours;
        const detectedTimeframe = hoursToTimeframe(detectedHours);
        // Always update to detected timeframe to stay in sync with backend
        if (detectedTimeframe !== analyzerTimeframe) {
          setAnalyzerTimeframe(detectedTimeframe);
          currentTimeframe = detectedTimeframe;
        }
      }

      // Create fresh msgContext with CURRENT timeframe (post-sync)
      const firstMsgContext = {};
      if (currentTimeframe) {
        firstMsgContext.modeLabel = "Fleet window";
        firstMsgContext.timeframeLabel = currentTimeframe;
      }
      
      setLogMessages((prev) => [
        ...prev,
        makeMessage("assistant", firstPrefix ? `${firstPrefix}\n\n${firstResult.answer}` : firstResult.answer, "", firstMsgContext),
      ]);

      if (firstResult.response.ok) {
        setFleetPage(Number(firstPagination?.page || page || 1));
        setLatestPagination(firstPagination);
        setLatestSessionsPagination(firstResult.data?.sessions_pagination || null);
      } else {
        setLatestPagination(null);
        setLatestSessionsPagination(null);
      }

      if (firstResult.response.ok && firstResult.data?.anchors) {
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
        && autoPaginateRecommended
        && autoPaginate
        && (Number(firstPageInfo?.total_pages || 1) > 1 || Boolean(firstPageInfo?.has_next_page));

      if (shouldAutoLoadRemainingPages) {
        const totalPages = Number(firstPageInfo?.total_pages || 1);
        let hasNextPage = Boolean(firstPageInfo?.has_next_page) || totalPages > 1;
        let loadedPages = Number(firstPageInfo?.page || 1);
        setAutoPageProgress({ loadedPages, totalPages });

        const hardPageCap = Math.max(totalPages, loadedPages + 20);
        for (let nextPage = loadedPages + 1; nextPage <= hardPageCap; nextPage += 1) {
          if (!hasNextPage && nextPage > totalPages) {
            break;
          }
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

          const nextPagination = nextResult.data?.pagination || null;
          const nextSessionsPagination = nextResult.data?.sessions_pagination || null;
          const nextMergedSessionsPagination = nextResult.data?.merged?.sessions_pagination_context || null;
          const nextMergedPagination = nextResult.data?.merged?.pagination_context || null;
          const nextPageInfo = nextSessionsPagination || nextPagination || nextMergedSessionsPagination || nextMergedPagination;

          loadedPages = Number(nextPageInfo?.page || nextPage);
          const nextTotalPages = Number(nextPageInfo?.total_pages || totalPages || loadedPages);
          hasNextPage = Boolean(nextPageInfo?.has_next_page) || loadedPages < nextTotalPages;
          setFleetPage(loadedPages);
          setLatestPagination(nextPagination);
          setLatestSessionsPagination(nextSessionsPagination);
          setLatestMerged(nextResult.data?.merged || null);
          setAutoPageProgress({ loadedPages, totalPages: nextTotalPages });
          
          // Sync timeframe from backend response for each page  
          let nextPageTimeframe = currentTimeframe;
          if (nextResult.data?.detected_lookback_hours !== undefined) {
            const detectedHours = nextResult.data.detected_lookback_hours;
            const detectedTimeframe = hoursToTimeframe(detectedHours);
            if (detectedTimeframe !== currentTimeframe) {
              nextPageTimeframe = detectedTimeframe;
              setAnalyzerTimeframe(detectedTimeframe);
            }
          }
          
          // Use synced timeframe for message context
          const nextMsgContext = {};
          if (nextPageTimeframe) {
            nextMsgContext.modeLabel = "Fleet window";
            nextMsgContext.timeframeLabel = nextPageTimeframe;
          }
          setLogMessages((prev) => [
            ...prev,
            makeMessage("assistant", `Page ${loadedPages} of ${nextTotalPages}\n\n${nextResult.answer}`, "", nextMsgContext),
          ]);

          if (!hasNextPage && loadedPages >= nextTotalPages) {
            break;
          }
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
    if (!lastFleetQuestion || busyLog) {
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

  // Component rendering is now handled by extracted components
  // All state management and handlers remain in App.jsx

  const isAssistantMode = mode === "assistant";
  const activeMessages = isAssistantMode ? messages : logMessages;
  const activeBusy = isAssistantMode ? busyChat : busyLog;
  const isBusy = activeBusy;
  const activeQuestion = isAssistantMode ? prompt : logQuestion;
  const activeCharCount = isAssistantMode ? prompt.length : logQuestion.length;
  const activeStopLabel = isAssistantMode ? "Stop response" : "Stop analysis";
  const shellClassName = [
    "chat-shell",
    isAssistantMode ? "assistant-only" : "",
    !isAssistantMode ? "analyst-shell" : "",
  ].filter(Boolean).join(" ");

  return (
    <main className="sensei-shell">
      <Topbar
        isAssistantMode={isAssistantMode}
        onModeChange={setMode}
        profile={profile}
        user={user}
        onNewSession={resetToNewSession}
        onSignOut={signOut}
      />

      <section className={shellClassName}>
        {!isAssistantMode && (
          <>
            <button
              type="button"
              className="help-button help-button-shell"
              onClick={() => {
                const nextOpen = !helpOpen;
                setHelpOpen(nextOpen);
                if (!nextOpen) {
                  sessionStorage.setItem(ANALYZER_HELP_DISMISSED_KEY, "1");
                }
              }}
            >
              {helpOpen ? "Close" : "Help"}
            </button>
          </>
        )}

        <div className="chat-center">
          <HelpPanel
            sections={ANALYZER_HELP_SECTIONS}
            isVisible={helpOpen && !isAssistantMode}
            onClose={() => setHelpOpen(false)}
          />

          <div className="chat-feed">
            <MessageList
              messages={activeMessages}
              isLoading={activeBusy}
              viewportRef={messagesViewportRef}
              onScroll={handleMessagesScroll}
              onCopy={copyToClipboard}
              copiedId={copiedId}
            />
            {!isAssistantMode && !analyzerSetupComplete && (
              <AnalyzerSetup
                setupStep={analyzerSetupStep}
                selectedTimeframe={analyzerTimeframe}
                onSelectTimeframe={handleSelectAnalyzerTimeframe}
                onChangeMode={handleChangeAnalyzerMode}
                isProcessing={busyLog}
              />
            )}
          </div>

          <Composer
            value={activeQuestion}
            onChange={(e) => {
              if (isAssistantMode) {
                setPrompt(e.target.value);
                return;
              }
              setLogQuestion(e.target.value);
            }}
            onKeyDown={handleComposerKeyDown}
            onSubmit={isAssistantMode ? sendPrompt : sendLogQuestion}
            placeholder={
              isAssistantMode
                ? "Ask anything"
                : analyzerSetupComplete
                ? "Ask your analysis question..."
                : "Answer the setup questions..."
            }
            disabled={activeBusy}
            charCount={activeCharCount}
            maxLength={4000}
            isAnalyzerMode={!isAssistantMode}
            showChangeMode={false}
            onChangeMode={handleChangeAnalyzerMode}
            suggestions={[]}
            suggestionsOpen={false}
            onToggleSuggestions={undefined}
            onApplySuggestion={undefined}
            isBusy={activeBusy}
            stopLabel={activeStopLabel}
            onStop={stopCurrentGeneration}
            enableSuggestionsButton={ENABLE_SUGGESTIONS_BUTTON}
          />
        </div>
      </section>
    </main>
  );
}

export default App;
