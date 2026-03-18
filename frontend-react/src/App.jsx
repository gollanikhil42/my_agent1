import { useEffect, useMemo, useState } from "react";
import { fetchAuthSession, fetchUserAttributes } from "aws-amplify/auth";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL;

function stamp() {
  return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
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
  const [chatSessionId] = useState(() => crypto.randomUUID());
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
  const [logMessages, setLogMessages] = useState([
    {
      id: crypto.randomUUID(),
      role: "system",
      text: "Session Log Analyst ready. Provide request/session/trace anchor and ask a question.",
      variant: "status",
      timestamp: stamp(),
    },
  ]);
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
  const [copiedId, setCopiedId] = useState("");
  const [profile, setProfile] = useState({ name: user?.username || "User", department: "", role: "" });

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

  const isBusy = busyChat || busyLog;

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
        },
        body: JSON.stringify({ prompt: trimmed, session_id: chatSessionId }),
      });

      const data = await response.json().catch(() => ({}));
      const answer = response.ok
        ? data.answer || "No answer returned."
        : data.error || data.message || `Request failed (${response.status})`;
      const trace = response.ok ? data.trace || {} : {};
      const improvedAnswer = response.ok ? data.answer_after_prompt_update : null;
      const evaluator = response.ok ? data?.evaluator?.result : null;
      const postEval = response.ok ? data?.post_update_evaluator?.result : null;

      setMessages((prev) => {
        const next = [...prev, makeMessage("assistant", answer)];

        if (trace?.request_id || trace?.session_id || trace?.xray_trace_id) {
          next.push({
            ...makeMessage(
              "system",
              `Trace: request_id=${trace.request_id || "n/a"} | session_id=${trace.session_id || "n/a"} | evaluator_session_id=${trace.evaluator_session_id || "n/a"}${trace.runtime_session_id ? ` | runtime_session_id=${trace.runtime_session_id}` : ""} | xray_trace_id=${trace.xray_trace_id || "n/a"}`,
              "metric",
            ),
            variant: "metric",
          });
        }

        if (evaluator?.evaluation?.scores) {
          const s = evaluator.evaluation.scores;
          next.push({
            ...makeMessage(
              "system",
              `Quality score: ${s.overall} | Accuracy ${s.accuracy} | Clarity ${s.clarity} | Helpfulness ${s.helpfulness}`,
              "metric",
            ),
            variant: "metric",
          });
        }

        if (improvedAnswer) {
          next.push(makeMessage("assistant", improvedAnswer, "bot-updated"));
        }

        if (postEval?.evaluation?.scores) {
          const ps = postEval.evaluation.scores;
          next.push({
            ...makeMessage("system", `Post-update score: ${ps.overall} (after prompt refinement)`, "metric"),
            variant: "metric",
          });
        }

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

    if (!effectiveAnchors.xray_trace_id) {
      setLogMessages((prev) => [
        ...prev,
        makeMessage("system", "No trace ID available yet. Send one assistant prompt first to capture a trace ID.", "metric"),
      ]);
      return;
    }

    setLogQuestion("");
    setBusyLog(true);
    setLogMessages((prev) => [
      ...prev,
      makeMessage("user", trimmed),
    ]);

    try {
      const session = await fetchAuthSession();
      const jwtToken = session.tokens?.idToken?.toString() || "";
      const response = await fetch(`${API_BASE_URL}/session-insights`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: jwtToken ? `Bearer ${jwtToken}` : "",
        },
        body: JSON.stringify({
          question: trimmed,
          request_id: effectiveAnchors.request_id,
          client_request_id: effectiveAnchors.client_request_id,
          session_id: effectiveAnchors.session_id,
          evaluator_session_id: effectiveAnchors.evaluator_session_id,
          xray_trace_id: effectiveAnchors.xray_trace_id,
          lookback_hours: 48,
        }),
      });

      const data = await response.json().catch(() => ({}));
      const answer = response.ok
        ? data.answer || "No analysis returned."
        : data.error || data.message || `Request failed (${response.status})`;

      setLogMessages((prev) => {
        const next = [...prev, makeMessage("assistant", answer)];

        if (response.ok && data?.anchors) {
          next.push({
            ...makeMessage(
              "system",
              `Anchors used: request_id=${data.anchors.request_id || "n/a"} | session_id=${data.anchors.session_id || "n/a"} | evaluator_session_id=${data.anchors.evaluator_session_id || "n/a"} | xray_trace_id=${data.anchors.xray_trace_id || "n/a"}`,
              "metric",
            ),
            variant: "metric",
          });
        }

        if (response.ok && data?.merged) {
          const merged = data.merged;
          const slow = merged?.xray?.slowest_step;
          const runtimeCount = merged?.runtime_records_found ?? 0;
          const evaluatorCount = merged?.evaluator_records_found ?? 0;
          next.push({
            ...makeMessage(
              "system",
              `Records: runtime=${runtimeCount}, evaluator=${evaluatorCount}${slow ? ` | slowest=${slow.name} (${slow.duration_ms} ms)` : ""}`,
              "metric",
            ),
            variant: "metric",
          });
        }

        return next;
      });

      if (response.ok && data?.anchors) {
        setAnalystAnchors((prev) => ({
          request_id: data.anchors.request_id || prev.request_id,
          client_request_id: data.anchors.client_request_id || prev.client_request_id,
          session_id: data.anchors.session_id || prev.session_id,
          evaluator_session_id: data.anchors.evaluator_session_id || prev.evaluator_session_id,
          xray_trace_id: data.anchors.xray_trace_id || prev.xray_trace_id,
        }));
      }
      setLatestMerged(response.ok ? data?.merged || null : null);
    } catch (error) {
      setLogMessages((prev) => [...prev, makeMessage("assistant", `Unable to fetch analysis. ${error?.message || ""}`.trim())]);
    } finally {
      setBusyLog(false);
    }
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
            <span className="title-gradient">my_agent1</span> Intelligence Dashboard
          </h1>
          <p className="subtitle">AI operations cockpit for live chat intelligence and session diagnostics.</p>
        </div>

        <nav className="mode-switch" role="tablist" aria-label="Console mode">
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
            Session Log Analyst
          </button>

          <div className="identity-pill">
            <span className="avatar">{profile.name.slice(0, 1).toUpperCase()}</span>
            <span className="online-dot" />
            <span className="identity-text">
              <strong>{profile.name}</strong>
              <small>{[profile.role, profile.department].filter(Boolean).join(" | ") || "Active"}</small>
            </span>
          </div>
          <button className="signout" onClick={signOut}>Sign out</button>
        </nav>
      </header>

      {mode === "assistant" ? (
        <section className="chat-card glass">
          {renderMessages(messages, busyChat)}

          <form className="composer" onSubmit={sendPrompt}>
            <textarea
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder="Ask anything..."
              rows={3}
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
      ) : (
        <section className="chat-card glass">
          <div className="anchors-head">
            <h2>Session Anchors</h2>
            <p>Trace ID is auto-captured from your last chat message. You can paste an older trace ID to query historical sessions.</p>
          </div>

          <div className="anchor-grid anchor-grid--single">
            <label>
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
          </div>

          {latestMerged && (
            <div className="insight-strip">
              <p>
                Runtime logs: <strong>{latestMerged.runtime_records_found || 0}</strong>
              </p>
              <p>
                Evaluator logs: <strong>{latestMerged.evaluator_records_found || 0}</strong>
              </p>
              <p>
                Slowest step: <strong>{latestMerged?.xray?.slowest_step?.name || "n/a"}</strong>
              </p>
            </div>
          )}

          {renderMessages(logMessages, busyLog)}

          <form className="composer" onSubmit={sendLogQuestion}>
            <textarea
              value={logQuestion}
              onChange={(e) => setLogQuestion(e.target.value)}
              placeholder="Ask diagnostics questions, e.g. Which step took the most time for this session?"
              rows={3}
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
      )}
    </main>
  );
}

export default App;
