import React, { useState } from 'react';
import './styles.css';

/**
 * ChatBubble Component
 * 
 * Renders a single message bubble with:
 * - Sent (blue, right-aligned) or inline (gray, left-aligned) variants
 * - Copy button with visual feedback
 * - Optional context badge (mode, timeframe, etc.)
 * - Avatar for assistant messages
 * - Markdown content support (via parent-level JSX)
 */

export const ChatBubble = ({
  children,
  variant = 'inline', // 'sent' or 'inline'
  contextBadge = null, // e.g., "Fleet Window, 1h"
  avatar = null, // React node for avatar icon
  onCopy = null,
}) => {
  const [copied, setCopied] = useState(false);

  const handleCopy = () => {
    // Extract plain text from children (simple fallback)
    const text = typeof children === 'string' ? children : children?.props?.children || '';
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
      if (onCopy) onCopy(text);
    });
  };

  const bubbleContainerClass = variant === 'sent' ? 'bubble-sent' : 'bubble-inline';

  return (
    <div className={`chat-bubble ${bubbleContainerClass}`}>
      {avatar && variant !== 'sent' && <div className="avatar">{avatar}</div>}
      
      <div
        className={`message ${variant === 'sent' ? 'message-sent' : 'message-inline'}`}
      >
        <div className="message-content">
          {children}
        </div>

        {contextBadge && (
          <div className="badge">
            {contextBadge}
          </div>
        )}

        <div className="message-actions">
          <button
            className="copy-button"
            onClick={handleCopy}
            title={copied ? 'Copied!' : 'Copy to clipboard'}
            aria-label="copy message"
          >
            {copied ? '✓ Copied' : '📋 Copy'}
          </button>
        </div>
      </div>
    </div>
  );
};

export default ChatBubble;
