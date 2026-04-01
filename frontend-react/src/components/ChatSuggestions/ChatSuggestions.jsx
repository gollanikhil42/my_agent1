import React from 'react';
import './styles.css';

/**
 * ChatSuggestions Component
 * 
 * Renders quick-action suggestion buttons.
 * - Supports vertical (default) and horizontal (inline) layouts
 * - Shows spinner during async operations
 * - Uses mode-specific suggestions (fleet vs single trace)
 * - Disabled state when processing
 */

export const ChatSuggestions = ({
  suggestions = [], // Array of { text: string, icon?: string }
  onSuggestionClick = () => {},
  isDisabled = false,
  inline = false,
  isLoading = false,
}) => {
  if (!suggestions || suggestions.length === 0) {
    return (
      <div className="empty-state">
        {isLoading ? 'Loading suggestions...' : 'No suggestions available'}
      </div>
    );
  }

  const containerClass = inline ? 'suggestions-inline' : 'suggestions-container';
  const buttonClass = inline ? 'inline-suggestion-button' : '';

  return (
    <div className={containerClass}>
      {suggestions.map((suggestion, index) => (
        <button
          key={index}
          className={`suggestion-button ${buttonClass}`}
          onClick={() => onSuggestionClick(suggestion.text)}
          disabled={isDisabled}
          title={suggestion.text}
        >
          {suggestion.icon && (
            <span className="suggestion-icon">{suggestion.icon}</span>
          )}
          <span className="suggestion-text">{suggestion.text}</span>
        </button>
      ))}
    </div>
  );
};

export default ChatSuggestions;
