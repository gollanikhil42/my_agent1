export function Composer({
  value,
  onChange,
  onKeyDown,
  onSubmit,
  placeholder,
  disabled,
  charCount,
  maxLength,
  isAnalyzerMode,
  showChangeMode,
  onChangeMode,
  suggestions,
  suggestionsOpen,
  onToggleSuggestions,
  onApplySuggestion,
  isBusy,
  stopLabel,
  onStop,
  enableSuggestionsButton = false,
}) {
  return (
    <>
      {isAnalyzerMode && enableSuggestionsButton && (
        <div className="composer-suggestions-shell">
          {showChangeMode && (
            <button
              type="button"
              className="composer-change-mode-btn"
              onClick={onChangeMode}
              title="Reset and choose a different mode or timeframe"
            >
              Change mode
            </button>
          )}
          <button
            type="button"
            className={`composer-suggestions-toggle${suggestionsOpen ? " open" : ""}`}
            onClick={onToggleSuggestions}
            aria-expanded={suggestionsOpen}
            aria-controls="composer-suggestions-panel"
          >
            {suggestionsOpen ? "Hide prompt suggestions" : "Show prompt suggestions"}
          </button>
          {suggestionsOpen && (
            <div
              id="composer-suggestions-panel"
              className="composer-suggestions-panel"
            >
              {suggestions.map((suggestion, index) => (
                <button
                  key={suggestion}
                  type="button"
                  className="suggestion-chip"
                  onClick={() => onApplySuggestion(suggestion)}
                  disabled={isBusy}
                >
                  <span className="suggestion-index">0{index + 1}</span>
                  <span className="suggestion-text">{suggestion}</span>
                </button>
              ))}
            </div>
          )}
        </div>
      )}

      <form className="floating-composer" onSubmit={onSubmit}>
        <textarea
          value={value}
          onChange={onChange}
          onKeyDown={onKeyDown}
          placeholder={placeholder}
          maxLength={maxLength}
          disabled={disabled}
        />
        <div className="composer-row">
          <p className="char-count">
            {charCount}/{maxLength}
          </p>
          <div className="composer-actions">
            <button
              className={`send${isBusy ? " stop-mode" : ""}`}
              type={isBusy ? "button" : "submit"}
              onClick={isBusy ? onStop : undefined}
              title={isBusy ? stopLabel : "Send"}
              aria-label={isBusy ? stopLabel : "Send"}
              disabled={isBusy ? false : disabled}
            >
              <span className="plane" aria-hidden="true">
                {isBusy ? "■" : "➤"}
              </span>
            </button>
          </div>
        </div>
      </form>
    </>
  );
}
