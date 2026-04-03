import { useState } from "react";
import "./ResponseMetrics.css";

export function ResponseMetrics({ metrics }) {
  const [isOpen, setIsOpen] = useState(false);

  if (!metrics || Object.keys(metrics).length === 0) {
    return null;
  }

  const {
    sessions_extracted = 0,
    sessions_shown = 0,
    data_completeness = "100%",
    accuracy = "high",
    reason = "",
    records_loss = 0,
  } = metrics;

  return (
    <div className="response-metrics-container">
      <button
        className="metrics-info-button"
        onClick={() => setIsOpen(!isOpen)}
        title="View response quality metrics"
        aria-label="Response metrics"
      >
        <span className="metrics-icon">ℹ️</span>
      </button>

      {isOpen && (
        <div className="metrics-popover">
          <div className="metrics-header">
            <h4>Response Quality Metrics</h4>
            <button
              className="close-btn"
              onClick={() => setIsOpen(false)}
              title="Close metrics"
            >
              ✕
            </button>
          </div>

          <div className="metrics-grid">
            <div className="metric-item">
              <span className="metric-label">Sessions Extracted</span>
              <span className="metric-value">{sessions_extracted}</span>
            </div>
            <div className="metric-item">
              <span className="metric-label">Sessions Shown</span>
              <span className="metric-value">{sessions_shown}</span>
            </div>
            <div className="metric-item">
              <span className="metric-label">Data Completeness</span>
              <span className={`metric-value completeness-${data_completeness.replace("%", "")}`}>
                {data_completeness}
              </span>
            </div>
            <div className="metric-item">
              <span className="metric-label">Accuracy Level</span>
              <span className={`metric-value accuracy-${accuracy}`}>{accuracy}</span>
            </div>
            {records_loss > 0 && (
              <div className="metric-item warning">
                <span className="metric-label">Records Loss</span>
                <span className="metric-value">{records_loss} sessions</span>
              </div>
            )}
          </div>

          {reason && (
            <div className="metrics-reason">
              <strong>Analysis Method:</strong>
              <p>{reason}</p>
            </div>
          )}

          <div className="metrics-footer">
            <p className="metrics-disclaimer">
              These metrics indicate how much of your data was used to generate this response.
              Higher completeness = more accurate analysis.
            </p>
          </div>
        </div>
      )}
    </div>
  );
}
