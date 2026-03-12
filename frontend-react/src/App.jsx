import { useEffect, useState } from "react";
import { fetchAuthSession, fetchUserAttributes } from "aws-amplify/auth";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL;

function App({ signOut, user }) {
  const [prompt, setPrompt] = useState("");
  const [messages, setMessages] = useState([
    {
      role: "system",
      text: "Connected. Ask anything to start chatting.",
      variant: "status",
    },
  ]);
  const [busy, setBusy] = useState(false);
  const [profile, setProfile] = useState({ name: user?.username || "User", department: "", role: "" });

  const turns = Math.floor(messages.filter((m) => m.role === "user").length);
  const upgrades = messages.filter((m) => m.variant === "bot-updated").length;

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

  async function sendPrompt(event) {
    event.preventDefault();
    const trimmed = prompt.trim();
    if (!trimmed || busy) {
      return;
    }

    setPrompt("");
    setBusy(true);
    setMessages((prev) => [
      ...prev,
      { role: "user", text: trimmed },
      { role: "system", text: "Analyzing request...", variant: "status" },
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
        body: JSON.stringify({ prompt: trimmed }),
      });

      const data = await response.json().catch(() => ({}));
      const answer = response.ok
        ? data.answer || "No answer returned."
        : data.error || data.message || `Request failed (${response.status})`;
      const improvedAnswer = response.ok ? data.answer_after_prompt_update : null;
      const evaluator = response.ok ? data?.evaluator?.result : null;
      const postEval = response.ok ? data?.post_update_evaluator?.result : null;

      setMessages((prev) => {
        const withoutThinking = prev.filter(
          (msg, idx) => !(msg.role === "system" && msg.variant === "status" && idx === prev.length - 1),
        );
        const next = [...withoutThinking, { role: "bot", text: answer }];

        if (evaluator?.evaluation?.scores) {
          const s = evaluator.evaluation.scores;
          next.push({
            role: "system",
            variant: "metric",
            text: `Quality score: ${s.overall} | Accuracy ${s.accuracy} | Clarity ${s.clarity} | Helpfulness ${s.helpfulness}`,
          });
        }

        if (improvedAnswer) {
          next.push({ role: "bot", variant: "bot-updated", text: improvedAnswer });
        }

        if (postEval?.evaluation?.scores) {
          const ps = postEval.evaluation.scores;
          next.push({
            role: "system",
            variant: "metric",
            text: `Post-update score: ${ps.overall} (after prompt refinement)`,
          });
        }

        return next;
      });
    } catch (error) {
      setMessages((prev) => {
        const withoutThinking = prev.filter(
          (msg, idx) => !(msg.role === "system" && msg.variant === "status" && idx === prev.length - 1),
        );
        return [...withoutThinking, { role: "bot", text: `Unable to fetch response. ${error?.message || ""}`.trim() }];
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="shell">
      <header className="hero">
        <div className="hero-top">
          <p className="badge">Adaptive Agent Console</p>
          <button className="signout" onClick={signOut}>Sign out</button>
        </div>
        <h1>my_agent1 Intelligence Dashboard</h1>
        <p className="subtitle">Real-time response generation, evaluation feedback, and prompt evolution.</p>

        <div className="hero-grid">
          <div className="profile">
            <div className="avatar">{profile.name.slice(0, 1).toUpperCase()}</div>
            <div>
              <p className="profile-name">{profile.name}</p>
              <p className="profile-meta">{[profile.role, profile.department].filter(Boolean).join(" | ") || "Signed in"}</p>
            </div>
          </div>
          <div className="mini-stat">
            <p className="mini-label">Turns</p>
            <p className="mini-value">{turns}</p>
          </div>
          <div className="mini-stat">
            <p className="mini-label">Prompt Upgrades</p>
            <p className="mini-value">{upgrades}</p>
          </div>
          <div className="mini-stat">
            <p className="mini-label">Session State</p>
            <p className="mini-value">{busy ? "Processing" : "Ready"}</p>
          </div>
        </div>
      </header>

      <section className="chat-card">
        <div className="messages">
          {messages.map((msg, i) => (
            <article key={i} className={`message ${msg.role} ${msg.variant || ""}`}>
              <div className="bubble">{msg.text}</div>
            </article>
          ))}
        </div>

        <form className="composer" onSubmit={sendPrompt}>
          <textarea
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder="Ask anything..."
            rows={2}
            disabled={busy}
          />
          <button className="send" type="submit" disabled={busy}>Send</button>
        </form>
      </section>
    </main>
  );
}

export default App;
