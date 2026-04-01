export function Topbar({ isAssistantMode, onModeChange, profile, user, onNewSession, onSignOut }) {
  return (
    <header className="sensei-topbar">
      <div className="sensei-brand">
        <strong>PHILIPS</strong>
        <span>SENSEI</span>
      </div>

      <nav className="sensei-nav" role="tablist" aria-label="Console mode">
        <button
          type="button"
          className={`nav-tab ${isAssistantMode ? "active" : ""}`}
          onClick={() => onModeChange("assistant")}
        >
          Assistant chat
        </button>
        <button
          type="button"
          className={`nav-tab ${!isAssistantMode ? "active" : ""}`}
          onClick={() => onModeChange("session")}
        >
          Analyser chat
        </button>
      </nav>

      <div className="sensei-user">
        <span>{(profile.name || user?.username || "JD").slice(0, 2).toUpperCase()}</span>
        <button className="new-session" onClick={onNewSession} type="button">New session</button>
        <button className="signout" onClick={onSignOut} type="button">Sign out</button>
      </div>
    </header>
  );
}
