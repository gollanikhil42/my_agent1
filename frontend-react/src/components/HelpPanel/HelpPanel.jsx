export function HelpPanel({ sections, isVisible, onClose }) {
  if (!isVisible) return null;

  return (
    <section className="help-panel" aria-label="Analyzer help">
      <div className="help-panel-header">
        <strong>Analyzer help</strong>
      </div>
      <div className="help-panel-body">
        {sections.map((section) => (
          <div key={section.title} className="help-block">
            <p className="help-title">{section.title}</p>
            <p className="help-text">{section.text}</p>
          </div>
        ))}
      </div>
    </section>
  );
}
