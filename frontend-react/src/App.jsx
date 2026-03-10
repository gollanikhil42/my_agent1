import { useEffect, useState } from "react";
import { fetchAuthSession, fetchUserAttributes } from "aws-amplify/auth";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL;

function App({ signOut, user }) {
  const [prompt, setPrompt] = useState("");
  const [messages, setMessages] = useState([
    { role: "system", text: "Connected. Ask anything to start chatting." },
  ]);
  const [busy, setBusy] = useState(false);
  const [profile, setProfile] = useState({ name: user?.username || "User", department: "", role: "" });

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
    setMessages((prev) => [...prev, { role: "user", text: trimmed }, { role: "system", text: "Thinking..." }]);

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
      const answer = response.ok ? data.answer || "No answer returned." : data.error || "Unable to fetch response.";

      setMessages((prev) => {
        const withoutThinking = prev.filter((msg, idx) => !(msg.role === "system" && msg.text === "Thinking..." && idx === prev.length - 1));
        return [...withoutThinking, { role: "bot", text: answer }];
      });
    } catch {
      setMessages((prev) => {
        const withoutThinking = prev.filter((msg, idx) => !(msg.role === "system" && msg.text === "Thinking..." && idx === prev.length - 1));
        return [...withoutThinking, { role: "bot", text: "Unable to fetch response." }];
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="shell">
      <header className="hero">
        <p className="badge">AgentCore Runtime</p>
        <h1>my_agent1 Interactive Console</h1>
        <div className="profile">
          <div className="avatar">{profile.name.slice(0, 1).toUpperCase()}</div>
          <div>
            <p className="profile-name">{profile.name}</p>
            <p className="profile-meta">{[profile.role, profile.department].filter(Boolean).join(" | ") || "Signed in"}</p>
          </div>
          <button className="signout" onClick={signOut}>Sign out</button>
        </div>
      </header>

      <section className="chat-card">
        <div className="messages">
          {messages.map((msg, i) => (
            <article key={i} className={`message ${msg.role}`}>
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
