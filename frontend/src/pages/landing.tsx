// Public landing page (/): what Aether Link is, how it works, the team, and a guest ask box
// over the showcase documents an admin selected. Ask-only: answers cite and highlight the
// source passage; documents themselves are never shown in full or downloadable.
import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import {
  ArrowRight,
  BookOpenText,
  Cpu,
  FileSearch,
  FileText,
  Fingerprint,
  Layers,
  LockKeyhole,
  MessageSquareText,
  ScanText,
  Send,
  ShieldCheck,
  Sparkles,
  TriangleAlert,
  Users,
} from 'lucide-react';
import Logo from '../components/Logo';
import { useSession } from '../hooks/useSession';
import { getErrorMessage, hasStatus } from '../services/api';
import { askPublic, listShowcase, PUBLIC_QUESTION_MAX, type PublicAnswer, type PublicSource } from '../services/publicService';

const FEATURES = [
  { icon: LockKeyhole, title: 'Private by design', text: 'Every user has an isolated knowledge base. PostgreSQL row-level security checks every passage before the AI sees it.' },
  { icon: Cpu, title: 'Runs fully local', text: 'Embeddings, OCR and the answer model run on our own server. No document text is sent to an outside AI service.' },
  { icon: BookOpenText, title: 'Answers with sources', text: 'Every answer cites numbered passages, and each statement is checked against its source before you see it.' },
  { icon: ScanText, title: 'Reads images too', text: 'Text inside images in PDF and Word files is read with OCR and placed where the image sat on the page.' },
  { icon: ShieldCheck, title: 'Safe uploads', text: 'Virus scanning, type checks and parsing in a locked-down sandbox container with no network access.' },
  { icon: Layers, title: 'Compares documents', text: 'Ask how two policies differ: matching statements are found and marked as agreeing, differing or contradicting.' },
];

const PIPELINE = [
  { icon: FileText, title: 'Upload', text: 'Checked, virus-scanned and de-duplicated' },
  { icon: ScanText, title: 'Read', text: 'Parsed in a sandbox, images read with OCR' },
  { icon: Fingerprint, title: 'Index', text: 'Split into passages and turned into meaning-vectors' },
  { icon: FileSearch, title: 'Retrieve', text: 'Closest passages you are allowed to see' },
  { icon: Sparkles, title: 'Answer', text: 'Local model answers only from those passages' },
  { icon: ShieldCheck, title: 'Verify', text: 'Every statement checked against its source' },
];

const TEAM = [
  { name: 'Muhammad Ayyan', role: 'Team lead · AI pipeline' },
  { name: 'Muhammad Usman', role: 'Search & retrieval' },
  { name: 'Musaab Mohamed', role: 'Backend · processing' },
  { name: 'Ahmed Sultan', role: 'Backend · security' },
  { name: 'Faisal Saleh', role: 'Frontend' },
  { name: 'Muhammad Umair', role: 'Frontend' },
];

const initials = (name: string) => name.split(' ').map((p) => p[0]).slice(0, 2).join('');

/** The excerpt with its supporting sentence wrapped in <mark>. */
export function HighlightedExcerpt({ text, highlight }: { text: string; highlight: string | null }) {
  const at = highlight ? text.indexOf(highlight) : -1;
  if (!highlight || at < 0) return <>{text}</>;
  return (
    <>
      {text.slice(0, at)}
      <mark>{highlight}</mark>
      {text.slice(at + highlight.length)}
    </>
  );
}

/** Answer text with [n] markers turned into buttons that point at the source card. */
function AnswerText({ text, onCite }: { text: string; onCite: (marker: number) => void }) {
  const parts: ReactNode[] = [];
  const re = /\[(\d{1,3})\]/g;
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text))) {
    if (m.index > last) parts.push(text.slice(last, m.index));
    const marker = Number(m[1]);
    parts.push(
      <button key={m.index} type="button" className="cite" onClick={() => onCite(marker)} aria-label={`Source ${marker}`}>
        {marker}
      </button>,
    );
    last = m.index + m[0].length;
  }
  parts.push(text.slice(last));
  return <p className="landing-answer__text">{parts}</p>;
}

function SourceCard({ source, active }: { source: PublicSource; active: boolean }) {
  return (
    <li id={`source-${source.marker}`} className={`source-card${active ? ' source-card--active' : ''}`} data-testid="source-card">
      <div className="source-card__head">
        <span className="cite cite--static">{source.marker}</span>
        <strong>{source.document_name}</strong>
        {source.page ? <small>page {source.page}</small> : null}
        {source.source_type === 'ocr' ? <small>text from an image</small> : null}
      </div>
      <blockquote>
        <HighlightedExcerpt text={source.text} highlight={source.highlight} />
      </blockquote>
    </li>
  );
}

function AskBox() {
  const [documents, setDocuments] = useState<string[] | null>(null);
  const [question, setQuestion] = useState('');
  const [asked, setAsked] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState<PublicAnswer | null>(null);
  const [active, setActive] = useState<number | null>(null);
  const sourcesRef = useRef<HTMLUListElement>(null);

  useEffect(() => {
    listShowcase().then(setDocuments).catch(() => setDocuments([]));
  }, []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const q = question.trim();
    if (!q || loading) return;
    setLoading(true);
    setError('');
    setResult(null);
    setActive(null);
    setAsked(q);
    try {
      setResult(await askPublic(q));
    } catch (err) {
      if (hasStatus(err, 429)) setError('You have asked a lot of questions. Please wait a moment, or sign in to keep asking.');
      else setError(getErrorMessage(err, 'The assistant is unavailable right now. Please try again shortly.'));
    } finally {
      setLoading(false);
    }
  }

  function cite(marker: number) {
    setActive(marker);
    sourcesRef.current?.querySelector(`#source-${marker}`)?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  const empty = documents !== null && documents.length === 0;

  return (
    <div className="landing-ask">
      <div className="landing-ask__docs">
        <span>You can ask about:</span>
        {documents === null && <em>loading…</em>}
        {empty && <em>No public documents are available yet.</em>}
        {documents?.map((name) => (
          <span key={name} className="doc-chip">
            <FileText size={13} /> {name}
          </span>
        ))}
      </div>

      <form className="landing-ask__form" onSubmit={submit}>
        <MessageSquareText size={19} aria-hidden="true" />
        <input
          aria-label="Ask a question"
          placeholder={empty ? 'Questions open once documents are published' : 'Ask a question about these documents…'}
          value={question}
          maxLength={PUBLIC_QUESTION_MAX}
          disabled={empty}
          onChange={(e) => setQuestion(e.target.value)}
        />
        <button type="submit" className="button button--primary" disabled={!question.trim() || loading || empty}>
          {loading ? 'Thinking…' : 'Ask'} <Send size={15} />
        </button>
      </form>

      {loading && (
        <div className="landing-answer landing-answer--loading" aria-live="polite">
          <span className="typing"><i /><i /><i /></span> Searching the documents and checking every statement…
        </div>
      )}
      {error && (
        <div className="landing-answer landing-answer--error" role="alert">
          <TriangleAlert size={17} /> {error}
        </div>
      )}
      {result && (
        <div className="landing-answer" aria-live="polite" data-testid="public-answer">
          <p className="landing-answer__question">{asked}</p>
          {result.insufficient ? (
            <p className="landing-answer__text muted-light">
              The published documents do not contain enough information to answer this question.
            </p>
          ) : (
            <AnswerText text={result.answer} onCite={cite} />
          )}
          {result.uncertain && !result.insufficient && (
            <p className="landing-answer__flag">
              <TriangleAlert size={14} /> {result.uncertain_reason || 'Please check the sources: this answer is less certain.'}
            </p>
          )}
          {result.sources.length > 0 && (
            <ul className="landing-sources" ref={sourcesRef}>
              {result.sources.map((s) => (
                <SourceCard key={s.marker} source={s} active={active === s.marker} />
              ))}
            </ul>
          )}
          <p className="landing-answer__note">
            Answers come only from the published documents. Only the cited passages are shown; documents cannot be downloaded.
          </p>
        </div>
      )}
    </div>
  );
}

export default function LandingPage() {
  const { isAuthenticated } = useSession();

  return (
    <main className="auth-page landing">
      <div className="auth-page__glow auth-page__glow--one" aria-hidden="true" />
      <div className="auth-page__glow auth-page__glow--two" aria-hidden="true" />

      <header className="auth-header">
        <Logo />
        <nav className="landing-nav">
          <a href="#ask">Ask</a>
          <a href="#how">How it works</a>
          <a href="#team">Team</a>
          {isAuthenticated ? (
            <Link to="/documents" className="button button--glass">
              Open workspace <ArrowRight size={17} className="button__arrow" />
            </Link>
          ) : (
            <>
              <Link to="/login" className="landing-nav__signin">Sign in</Link>
              <Link to="/signup" className="button button--glass">
                Create account <ArrowRight size={17} className="button__arrow" />
              </Link>
            </>
          )}
        </nav>
      </header>

      <section className="landing-hero">
        <span className="eyebrow fade-up"><ShieldCheck size={15} /> Secure document intelligence</span>
        <h1 className="fade-up" style={{ animationDelay: '100ms' }}>
          Ask your documents.
          <br />
          <span>Get answers you can check.</span>
        </h1>
        <p className="fade-up" style={{ animationDelay: '180ms' }}>
          Aether Link is a private, context-aware RAG system: upload PDF, Word and text files, ask questions in plain
          language, and get answers grounded in your own documents, with the exact source passage highlighted.
        </p>
        <div className="landing-hero__cta fade-up" style={{ animationDelay: '260ms' }}>
          <a href="#ask" className="button button--primary">Try it now <ArrowRight size={17} className="button__arrow" /></a>
          {!isAuthenticated && <Link to="/signup" className="button button--glass">Upload your own documents</Link>}
        </div>
      </section>

      <section id="ask" className="landing-section">
        <div className="landing-section__head">
          <h2>Ask the public documents</h2>
          <p>No account needed. Try a question about the documents below and see where every answer comes from.</p>
        </div>
        <AskBox />
      </section>

      <section className="landing-section">
        <div className="landing-section__head">
          <h2>What it does</h2>
        </div>
        <div className="feature-grid">
          {FEATURES.map(({ icon: Icon, title, text }) => (
            <article key={title} className="feature-card">
              <Icon size={22} />
              <h3>{title}</h3>
              <p>{text}</p>
            </article>
          ))}
        </div>
      </section>

      <section id="how" className="landing-section">
        <div className="landing-section__head">
          <h2>How it works</h2>
          <p>From upload to a verified, cited answer: every step runs on our own infrastructure.</p>
        </div>
        <ol className="pipeline">
          {PIPELINE.map(({ icon: Icon, title, text }, i) => (
            <li key={title}>
              <span className="pipeline__step">{i + 1}</span>
              <Icon size={20} />
              <strong>{title}</strong>
              <small>{text}</small>
            </li>
          ))}
        </ol>
        <div className="stack-row">
          {['FastAPI', 'PostgreSQL + RLS', 'ChromaDB', 'Redis', 'Ollama', 'nomic-embed-text', 'Qwen 2.5', 'Florence-2 OCR', 'React'].map((t) => (
            <span key={t}>{t}</span>
          ))}
        </div>
      </section>

      <section id="team" className="landing-section">
        <div className="landing-section__head">
          <h2><Users size={22} /> Team Aether-Link</h2>
          <p>Final International University · Software Engineering project</p>
        </div>
        <div className="team-grid">
          {TEAM.map((m) => (
            <div key={m.name} className="team-card">
              <span className="team-card__avatar">{initials(m.name)}</span>
              <strong>{m.name}</strong>
              <small>{m.role}</small>
            </div>
          ))}
        </div>
      </section>

      <footer className="auth-footer">
        <span>© {new Date().getFullYear()} Aether Link Tech</span>
        <span>Private infrastructure · Encrypted at rest · Local AI models</span>
      </footer>
    </main>
  );
}
