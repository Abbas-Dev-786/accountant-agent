const checks = [
  ["Xero Demo Company", "Direct adapter · ready"],
  ["Plaid Sandbox", "Cursor sync · ready"],
  ["Google test Workspace", "Evidence scope · ready"],
];

export default function Home() {
  return (
    <main>
      <section className="banner">DEMO — SYNTHETIC DATA</section>
      <header>
        <div>
          <p className="eyebrow">ACCOUNTINGOS / CLOSE READINESS</p>
          <h1>Prepare July 2026 for review.</h1>
          <p className="subtitle">
            A reviewable package begins only after every configured source is current and complete.
          </p>
        </div>
        <button>Prepare close package</button>
      </header>

      <section className="grid" aria-label="Demo workspace">
        <article className="card sources">
          <p className="eyebrow">SOURCE READINESS</p>
          <h2>All required sources</h2>
          <ul>
            {checks.map(([name, status]) => (
              <li key={name}>
                <span className="dot" aria-hidden="true" />
                <span><strong>{name}</strong><small>{status}</small></span>
                <b>Healthy</b>
              </li>
            ))}
          </ul>
        </article>

        <article className="card progress">
          <p className="eyebrow">CLOSE RUN</p>
          <h2>Waiting to synchronize</h2>
          <p>Provider watermarks and a frozen source snapshot will appear here. No local fallback is used.</p>
          <div className="steps"><span>1</span><i /><span>2</span><i /><span>3</span></div>
          <div className="labels"><span>Synchronize</span><span>Review</span><span>Approve draft</span></div>
        </article>

        <article className="card policy">
          <p className="eyebrow">ACTION POLICY</p>
          <h2>Bounded by design</h2>
          <p>Only a configured controller may approve a frozen package. Xero writes are balanced manual journals in <strong>DRAFT</strong> status.</p>
          <div className="forbidden">Posting, payments, deletion, and period locking are unavailable.</div>
        </article>
      </section>
    </main>
  );
}

