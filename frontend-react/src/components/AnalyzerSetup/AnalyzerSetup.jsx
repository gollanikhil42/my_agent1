import React from 'react';
import './styles.css';

/**
 * AnalyzerSetup Component (Simplified)
 * 
 * Now only asks for timeframe selection since fleet mode is the only mode.
 * Users can change timeframe mid-conversation using natural language.
 * 
 * Props:
 * - setupStep: 'awaiting_timeframe' | 'complete'
 * - selectedTimeframe: string (e.g., '1h', 'overall', null)
 * - onSelectTimeframe: (timeframe) => void
 * - onChangeMode: () => void - resets to timeframe selection
 */

const TIMEFRAME_OPTIONS = [
  { value: '30m', label: 'Last 30 minutes' },
  { value: '1h', label: 'Last 1 hour' },
  { value: '4h', label: 'Last 4 hours' },
  { value: '1d', label: 'Last 24 hours' },
  { value: 'overall', label: 'Overall' },
];

export const AnalyzerSetup = ({
  setupStep = 'awaiting_timeframe',
  selectedTimeframe = null,
  onSelectTimeframe = () => {},
  onChangeMode = () => {},
  isProcessing = false,
}) => {
  const renderTimeframeSelection = () => (
    <div className="setup-step">
      <h2 className="setup-title">Select timeframe</h2>
      <p className="setup-subtitle">
        Which time period should I analyze? (You can change this anytime by asking)
      </p>
      <div className="option-group">
        {TIMEFRAME_OPTIONS.map((opt) => (
          <button
            key={opt.value}
            className={`option-button ${selectedTimeframe === opt.value ? 'selected' : ''}`}
            onClick={() => onSelectTimeframe(opt.value)}
            disabled={isProcessing}
            style={{ flex: '1 1 calc(33.333% - 0.667rem)' }}
          >
            {opt.label}
          </button>
        ))}
      </div>
    </div>
  );

  const renderCompletion = () => (
    <div>
      <div className="completion-message">
        <span className="completion-icon">✓</span>
        <div>
          <strong>Ready!</strong> Analyzing fleet data for the {selectedTimeframe} timeframe. Ask any questions below.
        </div>
      </div>
      <button className="change-button" onClick={onChangeMode} style={{ marginTop: '1rem' }}>
        ← Change timeframe
      </button>
    </div>
  );

  return (
    <div className="setup-container">
      {setupStep === 'awaiting_timeframe' && renderTimeframeSelection()}
      {setupStep === 'complete' && renderCompletion()}
    </div>
  );
};

export default AnalyzerSetup;
