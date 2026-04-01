import React, { useRef, useEffect } from 'react';
import './styles.css';

/**
 * ChatInput Component
 * 
 * Renders a message input textarea with send/voice button toggle.
 * - Enter to send, Shift+Enter for newline
 * - Disabled state when processing
 * - Optional error message display
 * - Voice button placeholder (can integrate useRecorder hook)
 */

export const ChatInput = ({
  value = '',
  onChange = () => {},
  onSend = () => {},
  isDisabled = false,
  placeholder = 'Ask me anything...',
  error = '',
  autoFocus = false,
}) => {
  const textareaRef = useRef(null);

  useEffect(() => {
    if (autoFocus && textareaRef.current) {
      textareaRef.current.focus();
    }
  }, [autoFocus]);

  const handleKeyPress = (e) => {
    // Enter without Shift = send
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      if (value.trim() && !isDisabled) {
        onSend();
      }
    }
    // Shift+Enter = newline (default behavior)
  };

  const handleChange = (e) => {
    onChange(e.target.value);
  };

  return (
    <div className="chat-input">
      <div className="text-area-wrapper">
        <textarea
          ref={textareaRef}
          className="text-area"
          value={value}
          onChange={handleChange}
          onKeyDown={handleKeyPress}
          placeholder={placeholder}
          disabled={isDisabled}
          rows={3}
          aria-label="message input"
        />
        {error && <div className="error-text">{error}</div>}
      </div>

      <div className="button-wrapper">
        {value.trim() ? (
          <button
            className="send-button"
            onClick={onSend}
            disabled={isDisabled}
            aria-label="send message"
            title="Send (Enter)"
          >
            ➤
          </button>
        ) : (
          <button
            className="voice-button"
            disabled={true}
            aria-label="voice input"
            title="Voice input (coming soon)"
          >
            🎤
          </button>
        )}
      </div>
    </div>
  );
};

export default ChatInput;
