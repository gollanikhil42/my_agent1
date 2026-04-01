export function TypingIndicator() {
  return (
    <div className="typing" aria-live="polite" aria-label="Assistant is typing">
      <span />
      <span />
      <span />
    </div>
  );
}

export function MessageList({
  messages,
  isLoading,
  viewportRef,
  onScroll,
  onCopy,
  copiedId,
}) {
  return (
    <div className="messages" ref={viewportRef} onScroll={onScroll}>
      {messages.map((msg) => (
        <article key={msg.id} className={`message ${msg.role} ${msg.variant || ""}`}>
          <div className="bubble-wrap">
            <div className="bubble-header">
              <span className="who">
                {msg.role === "assistant"
                  ? "Assistant"
                  : msg.role === "user"
                  ? "You"
                  : "System"}
              </span>
              <span className="time">{msg.timestamp}</span>
            </div>
            <div className="bubble">{msg.text}</div>
            {msg.role === "assistant" && (
              <div className="bubble-actions">
                <button
                  className="copy-btn"
                  type="button"
                  onClick={() => onCopy(msg.text, msg.id)}
                  title="Copy message"
                >
                  {copiedId === msg.id ? "Copied" : "Copy"}
                </button>
                {msg.modeLabel && (
                  <span className="msg-context-badge">
                    {msg.modeLabel}
                    {msg.timeframeLabel ? ` · ${msg.timeframeLabel}` : ""}
                  </span>
                )}
              </div>
            )}
          </div>
        </article>
      ))}
      {isLoading && (
        <article className="message assistant typing-row">
          <div className="bubble-wrap">
            <div className="bubble-header">
              <span className="who">Assistant</span>
              <span className="time">{new Date().toLocaleTimeString()}</span>
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
