import { useEffect, useMemo, useState } from "react";
import { fetchAuthSession, fetchUserAttributes } from "aws-amplify/auth";

const SESSION_STORAGE_KEY = "session_id";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL;
const ANALYSIS_MAX_RETRIES = 2;
const RETRYABLE_STATUS = new Set([429, 502, 503]);
const DEFAULT_FLEET_PAGE_SIZE = 20;
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

async function postWithRetry(url, options, maxRetries = ANALYSIS_MAX_RETRIES) {
  let lastResponse = null;
  let lastError = null;

  for (let attempt = 0; attempt <= maxRetries; attempt += 1) {
    try {
      const response = await fetch(url, options);
      lastResponse = response;
      if (!RETRYABLE_STATUS.has(response.status) || attempt === maxRetries) {
        return response;
      }
    } catch (error) {
      lastError = error;
      if (attempt === maxRetries) {
        throw error;
      }
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
  const [chatSessionId] = useState(() => getOrCreateSessionId());
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
  const [textareaExpanded, setTextareaExpanded] = useState(true);
  const [chatDrawerOpen, setChatDrawerOpen] = useState(true);

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

  async function sendPrompt(event) {
    event.preventDefault();
    const trimmed = prompt.trim();
    if (!trimmed || isBusy) {
      return;
    }

    setPrompt("");
    setBusyChat(true);
       setTextareaExpanded(false);
    setMessages((prev) => [
      ...prev,
      makeMessage("user", trimmed),
    ]);

    try {
      const session = await fetchAuthSession();
      const jwtToken = session.tokens?.idToken?.toString() || "";

      const response = await fetch(`${API_BASE_URL}/chat`, {
        method: "POST",
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
      setMessages((prev) => [...prev, makeMessage("assistant", `Unable to fetch response. ${error?.message || ""}`.trim())]);
    } finally {
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
        });

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
      setAutoPageProgress(null);
      setLatestPagination(null);
      setLogMessages((prev) => [...prev, makeMessage("assistant", `Unable to fetch analysis. ${error?.message || ""}`.trim())]);
    } finally {
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

  function renderMessages(items, loading) {
    return (
      <div className="messages">
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

  return (
    <main className="shell">
      <header className="topbar glass">
        <div>
          <h1>
            <span className="title-gradient">Observability & Evaluations</span> Dashboard
          </h1>
          <p className="subtitle">AI operations cockpit for live chat intelligence and session diagnostics.</p>
        </div>

        <nav className={`mode-switch ${mode === "assistant" ? "mode-switch--assistant" : "mode-switch--session"}`} role="tablist" aria-label="Console mode">
          <button
            className={`mode-btn ${mode === "assistant" ? "active" : ""}`}
            onClick={() => setMode("assistant")}
            type="button"
          >
            Assistant Chat
          </button>
          <button
            className={`mode-btn ${mode === "session" ? "active" : ""}`}
            onClick={() => setMode("session")}
            type="button"
          >
            Analyser logs
          </button>

          <button className="signout" onClick={signOut}>Sign out</button>
        </nav>
      </header>

      {mode === "assistant" ? (
        <section className="workspace-layout workspace-layout--assistant">
          <aside className={`control-drawer glass ${chatDrawerOpen ? "open" : "collapsed"}`}>
            <div className="drawer-header">
              <div>
                <p className="drawer-eyebrow">Chat Profile</p>
                <h2>Quick Info</h2>
                <p>Your chat session, account details, and latest trace context.</p>
              </div>
              <button type="button" className="drawer-toggle" onClick={() => setChatDrawerOpen(false)}>
                Hide
              </button>
            </div>

            <div className="drawer-scroll">
              <div className="profile-card">
                <span className="profile-label">Signed in</span>
                <strong>{profile.name}</strong>
                <p>{[profile.role, profile.department].filter(Boolean).join(" | ") || "Chat session active"}</p>
              </div>

              <div className="control-stack">
                <label className="control-field">
                  <span>User</span>
                  <input
                    value={[profile.role, profile.department].filter(Boolean).join(" | ") || "Active"}
                    readOnly
                  />
                </label>
              </div>
            </div>
          </aside>

          <section className="chat-stage glass chat-stage--assistant">
            <div className="stage-header">
              <div className="stage-heading">
                <button type="button" className="drawer-toggle drawer-toggle--inline" onClick={() => setChatDrawerOpen((prev) => !prev)}>
                  {chatDrawerOpen ? "−" : "+"}
                </button>
                <h2>Assistant Chat</h2>
              </div>
            </div>

            {renderMessages(messages, busyChat)}

            <form className={`composer ${textareaExpanded ? "composer--expanded" : "composer--collapsed"}`} onSubmit={sendPrompt}>
              <textarea
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                onFocus={() => setTextareaExpanded(true)}
                onBlur={(e) => {
                  if (!e.currentTarget.value.trim()) {
                    setTextareaExpanded(false);
                  }
                }}
                placeholder={textareaExpanded ? "Ask anything..." : "Click to ask..."}
                maxLength={4000}
                disabled={isBusy}
              />
              <div className="composer-row">
                <p className="char-count">{prompt.length}/4000</p>
                <button className="send" type="submit" disabled={isBusy} title="Send message" aria-label="Send message">
                  <span className="plane" aria-hidden="true">&#10148;</span>
                </button>
              </div>
            </form>
          </section>
        </section>
      ) : (
        <section className="workspace-layout">
          <aside className={`control-drawer glass ${controlsOpen ? "open" : "collapsed"}`}>
            <div className="drawer-header">
              <div>
                <p className="drawer-eyebrow">Diagnostics controls</p>
                <h2>Session Anchors</h2>
                <p>{analystMode !== "single_trace" ? `Fleet mode analyzes ${logLookbackHours === "overall" ? "all available traces from start to now" : `all traces in the last ${selectedLookbackLabel}`} across X-Ray, runtime, and evaluator logs.` : "Trace ID is auto-captured from your last chat message. You can paste an older trace ID to query historical sessions."}</p>
              </div>
              <button type="button" className="drawer-toggle" onClick={() => setControlsOpen(false)}>
                Hide
              </button>
            </div>

            <div className="drawer-scroll">
              <div className="profile-card">
                <span className="profile-label">Signed in</span>
                <strong>{profile.name}</strong>
                <p>{[profile.role, profile.department].filter(Boolean).join(" | ") || "Analyst session active"}</p>
              </div>

              <div className="control-stack">
                <label className="control-field">
                  <span>Analyzer Mode</span>
                  <select
                    value={analystMode}
                    onChange={(e) => setAnalystMode(e.target.value)}
                  >
                    <option value="fleet_window">All traces (fleet window)</option>
                    <option value="single_trace">Single trace deep-dive</option>
                  </select>
                </label>

                <label className="control-field">
                  <span>Timeframe</span>
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
                  <label className="control-field">
                    <span>X-Ray Trace ID</span>
                    <div className="anchor-input-wrap">
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
                      <button
                        type="button"
                        className="copy-icon"
                        onClick={() => copyToClipboard(effectiveAnchors.xray_trace_id, "anchor-xray")}
                        title="Copy X-Ray Trace ID"
                      >
                        {copiedId === "anchor-xray" ? "Copied" : "Copy"}
                      </button>
                    </div>
                  </label>
                )}
              </div>
            </div>
          </aside>

          <section className="chat-stage glass">
            <div className="stage-header">
              <div className="stage-heading">
                <button type="button" className="drawer-toggle drawer-toggle--inline" onClick={() => setControlsOpen((prev) => !prev)}>
                  {controlsOpen ? "−" : "+"}
                </button>
                <h2>{isFleetMode ? "Fleet Analysis" : "Trace Deep-Dive"}</h2>
              </div>
            </div>

            {latestMerged && (
              <div className="insight-strip">
                {latestMerged?.analysis_mode === "fleet_window" || analystMode !== "single_trace" ? (
                  <>
                    <p>
                      Traces: <strong>{totalFleetTraces}</strong>
                    </p>
                    <p>
                      E2E avg: <strong>{latestMerged?.fleet_metrics?.e2e_ms?.avg || 0} ms</strong>
                    </p>
                    <p>
                      Top bottleneck: <strong>{latestMerged?.bottleneck_ranking?.[0]?.component || "n/a"}</strong>
                    </p>
                  </>
                ) : (
                  <>
                    <p>
                      Runtime logs: <strong>{latestMerged.runtime_records_found || 0}</strong>
                    </p>
                    <p>
                      Evaluator logs: <strong>{latestMerged.evaluator_records_found || 0}</strong>
                    </p>
                    <p>
                      Slowest step: <strong>{latestMerged?.xray?.slowest_step?.name || "n/a"}</strong>
                    </p>
                  </>
                )}
              </div>
            )}

            {isFleetMode && latestPagination && (
              <div className="pagination-strip">
                <p>
                  {autoPageProgress?.totalPages > 1 ? (
                    <>
                      Loaded <strong>{autoPageProgress.loadedPages}</strong> of <strong>{autoPageProgress.totalPages}</strong> pages automatically
                    </>
                  ) : (
                    <>
                      Page <strong>{latestPagination.page}</strong> of <strong>{latestPagination.total_pages}</strong>
                    </>
                  )}
                  {" "}
                  (<strong>{latestPagination.total_traces}</strong> total traces in this window, {latestPagination.traces_on_this_page} on this page)
                </p>
                <div className="pagination-actions">
                  <button
                    type="button"
                    className="mode-btn"
                    onClick={() => goToFleetPage(Math.max(1, fleetPage - 1))}
                    disabled={isBusy || latestPagination.page <= 1}
                  >
                    Previous
                  </button>
                  <button
                    type="button"
                    className="mode-btn"
                    onClick={() => goToFleetPage(fleetPage + 1)}
                    disabled={isBusy || !latestPagination.has_next_page}
                  >
                    Next
                  </button>
                </div>
              </div>
            )}

            {isFleetMode && latestSessionsPagination && (
              <div className="pagination-strip">
                <p>
                  {autoPageProgress?.totalPages > 1 ? (
                    <>
                      Loaded <strong>{autoPageProgress.loadedPages}</strong> of <strong>{autoPageProgress.totalPages}</strong> session pages automatically
                    </>
                  ) : (
                    <>
                      Sessions page <strong>{latestSessionsPagination.page}</strong> of <strong>{latestSessionsPagination.total_pages}</strong>
                    </>
                  )}
                  {" "}
                  (<strong>{latestSessionsPagination.total_sessions}</strong> total sessions, {latestSessionsPagination.sessions_on_this_page} on this page)
                </p>
                <div className="pagination-actions">
                  <button
                    type="button"
                    className="mode-btn"
                    onClick={() => goToFleetPage(Math.max(1, fleetPage - 1))}
                    disabled={isBusy || latestSessionsPagination.page <= 1}
                  >
                    Previous
                  </button>
                  <button
                    type="button"
                    className="mode-btn"
                    onClick={() => goToFleetPage(fleetPage + 1)}
                    disabled={isBusy || !latestSessionsPagination.has_next_page}
                  >
                    Next
                  </button>
                </div>
              </div>
            )}

            {renderMessages(logMessages, busyLog)}

            {isFleetMode && (
              <div className="prompt-suggestions">
                {[
                  "Give me a summary of all sessions in this time window and highlight any issues.",
                  "Which sessions are the slowest and what is causing the latency?",
                  "Which sessions had the most errors and what patterns do you see?",
                  "List all users and show their sessions along with any issues they faced.",
                  "Analyze the most problematic session in detail and explain what went wrong.",
                ].map((suggestion) => (
                  <button
                    key={suggestion}
                    type="button"
                    className="suggestion-chip"
                    onClick={() => setLogQuestion(suggestion)}
                    disabled={isBusy}
                  >
                    {suggestion}
                  </button>
                ))}
              </div>
            )}

            <form className="composer composer--expanded" onSubmit={sendLogQuestion}>
              <textarea
                value={logQuestion}
                onChange={(e) => setLogQuestion(e.target.value)}
                placeholder="Ask diagnostics questions, e.g. get the evaluator metric scores for trace 69c27b2d13ded83308a2dbbe15a019df and tell me the slowest X-Ray step"
                rows={6}
                maxLength={4000}
                disabled={isBusy}
              />
              <div className="composer-row">
                <p className="char-count">{logQuestion.length}/4000</p>
                <button className="send" type="submit" disabled={isBusy} title="Analyze session" aria-label="Analyze session">
                  <span className="plane" aria-hidden="true">&#10148;</span>
                </button>
              </div>
            </form>
          </section>
        </section>
      )}
    </main>
  );
}

export default App;
