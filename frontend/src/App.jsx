import React, { useState, useEffect, useMemo, useRef, useCallback } from "react";
import {
  CheckCircle2,
  Clock,
  XCircle,
  ExternalLink,
  Copy,
  Check,
  MapPin,
  Award,
  Bookmark,
  RefreshCw,
  Database,
  Activity,
  Sparkles,
  Search,
  X,
  ChevronDown,
  StickyNote,
  Timer,
  EyeOff,
  Eye,
} from "lucide-react";
import "./App.css";

// ─── Storage key prefixes ────────────────────────────────────────────────────
const APPLIED_PREFIX   = "recruitrecon:applied:";
const NOTES_PREFIX     = "recruitrecon:note:";
const COLLAPSED_PREFIX = "recruitrecon:collapsed:";

// ─── localStorage helpers ────────────────────────────────────────────────────

function readPrefixedFromStorage(prefix, parse = (v) => v) {
  const next = {};
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (!k?.startsWith(prefix)) continue;
      try {
        next[k.slice(prefix.length)] = parse(localStorage.getItem(k));
      } catch {
        // One corrupted entry (e.g. malformed JSON) shouldn't wipe every
        // other valid key under this prefix — skip just this one.
      }
    }
  } catch { /* localStorage unavailable (e.g. private browsing) */ }
  return next;
}

// ─── Hooks ───────────────────────────────────────────────────────────────────

function useApplied() {
  // Lazy init reads localStorage once — avoids calling setState inside an effect
  const [applied, setApplied] = useState(() => readPrefixedFromStorage(APPLIED_PREFIX, JSON.parse));

  const toggle = useCallback((id) => {
    setApplied((prev) => {
      const next = { ...prev };
      const key = APPLIED_PREFIX + id;
      if (next[id]) {
        delete next[id];
        localStorage.removeItem(key);
      } else {
        const rec = { applied: true, ts: new Date().toISOString() };
        next[id] = rec;
        localStorage.setItem(key, JSON.stringify(rec));
      }
      return next;
    });
  }, []);

  return [applied, toggle];
}

function useNotes() {
  const [notes, setNotes] = useState(() => readPrefixedFromStorage(NOTES_PREFIX));

  const setNote = useCallback((id, text) => {
    setNotes((prev) => {
      const next = { ...prev };
      const key = NOTES_PREFIX + id;
      if (!text.trim()) {
        delete next[id];
        localStorage.removeItem(key);
      } else {
        next[id] = text;
        localStorage.setItem(key, text);
      }
      return next;
    });
  }, []);

  return [notes, setNote];
}

function useCollapsed(sectionId, defaultCollapsed = false) {
  const key = COLLAPSED_PREFIX + sectionId;
  const [collapsed, setCollapsed] = useState(() => {
    try {
      const stored = localStorage.getItem(key);
      return stored !== null ? stored === "true" : defaultCollapsed;
    } catch { return defaultCollapsed; }
  });

  const toggle = useCallback(() => {
    setCollapsed((v) => {
      const next = !v;
      try { localStorage.setItem(key, String(next)); } catch { /* ignore */ }
      return next;
    });
  }, [key]);

  return [collapsed, toggle];
}

function useAutoRefresh(callback, intervalMs = 60_000) {
  const [enabled, setEnabled] = useState(false);

  useEffect(() => {
    if (!enabled) return;
    const id = setInterval(callback, intervalMs);
    return () => clearInterval(id);
  }, [enabled, intervalMs, callback]);

  return [enabled, setEnabled];
}

// ─── Utilities ───────────────────────────────────────────────────────────────

function urlDomain(url) {
  try { return new URL(url).hostname.replace(/^www\./, ""); }
  catch { return url || ""; }
}

function relativeDate(iso) {
  if (!iso) return "";
  const days = Math.floor((Date.now() - new Date(iso).getTime()) / 86_400_000);
  if (days === 0) return "today";
  if (days === 1) return "1d ago";
  if (days < 30)  return `${days}d ago`;
  if (days < 56)  return `${Math.floor(days / 7)}w ago`;
  return `${Math.floor(days / 30)}mo ago`;
}

// ─── Job filtering ───────────────────────────────────────────────────────────

function matchesQuery(job, q) {
  return (
    job.title?.toLowerCase().includes(q) ||
    job.company_name?.toLowerCase().includes(q) ||
    job.location?.toLowerCase().includes(q) ||
    job.certs_mentioned?.some((c) => c.toLowerCase().includes(q))
  );
}

// Shared by filteredJobs (applied against the active filter) and chipCounts
// (applied against every filter to compute badge counts). `applied` is only
// used by the "applied" predicate; the others ignore it.
const FILTER_PREDICATES = {
  new:     (j) => j.first_seen_at_run,
  tierA:   (j) => j.gpen_explicit && j.verification?.status === "verified_live",
  tierB:   (j) => !j.gpen_explicit && j.verification?.status === "verified_live",
  static:  (j) => j.is_static || j.verification?.status === "skipped",
  applied: (j, applied) => !!applied[j.id],
};

// Shared text-color classes for Section and StatBox (they use different
// border opacities, so only the text class is actually common between them).
const COLOR_TEXT = {
  emerald: "text-emerald-400",
  teal:    "text-teal-400",
  amber:   "text-amber-400",
  sky:     "text-sky-400",
  rose:    "text-rose-400",
  lime:    "text-lime-400",
};

// ─── Small atoms ─────────────────────────────────────────────────────────────

function CopyButton({ text }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setCopied(true);
          setTimeout(() => setCopied(false), 1400);
        } catch { /* clipboard may be unavailable */ }
      }}
      className="inline-flex items-center gap-1 text-stone-500 hover:text-teal-400 transition-colors text-[10px] font-mono"
      aria-label="Copy URL"
    >
      {copied
        ? <><Check className="w-3 h-3" /><span>copied</span></>
        : <><Copy className="w-3 h-3" /><span>copy</span></>}
    </button>
  );
}

function StatusPill({ status }) {
  const map = {
    verified_live:          { dot: "bg-emerald-400 animate-pulse", text: "text-emerald-400", label: "verified live" },
    closed_404:             { dot: "bg-rose-500",                  text: "text-rose-400",    label: "closed (404)" },
    closed_410:             { dot: "bg-rose-500",                  text: "text-rose-400",    label: "closed (410)" },
    closed_inactive_text:   { dot: "bg-rose-500",                  text: "text-rose-400",    label: "closed (page text)" },
    disappeared_from_api:   { dot: "bg-rose-500",                  text: "text-rose-400",    label: "removed from feed" },
    error:                  { dot: "bg-amber-400",                 text: "text-amber-400",   label: "verify error" },
    skipped:                { dot: "bg-sky-500",                   text: "text-sky-400",     label: "static / unverified" },
  };
  const m = map[status] ?? { dot: "bg-stone-500", text: "text-stone-400", label: status || "unknown" };
  return (
    <div className="inline-flex items-center gap-1.5">
      <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${m.dot}`} />
      <span className={`text-[10px] font-mono uppercase tracking-widest ${m.text}`}>{m.label}</span>
    </div>
  );
}

function TierABadge({ certs }) {
  const label = certs?.length > 0 ? certs.slice(0, 3).join(", ") : "tier a";
  return (
    <div className="inline-flex items-center gap-1 px-1.5 py-0.5 bg-teal-400/10 border border-teal-400/30 rounded text-teal-300 text-[10px] font-mono uppercase tracking-wider">
      <Award className="w-2.5 h-2.5" />{label}
    </div>
  );
}

function NewBadge() {
  return (
    <div className="inline-flex items-center gap-1 px-1.5 py-0.5 bg-lime-400/10 border border-lime-400/40 rounded text-lime-300 text-[10px] font-mono uppercase tracking-wider animate-pulse shadow-[0_0_6px_rgba(163,230,53,0.25)]">
      <Sparkles className="w-2.5 h-2.5" />new
    </div>
  );
}

// ─── Job Note ────────────────────────────────────────────────────────────────

function JobNote({ jobId, notes, setNote }) {
  const [open, setOpen] = useState(false);
  const taRef = useRef(null);
  const text  = notes[jobId] ?? "";
  const hasNote = !!text.trim();

  const handleToggle = () => {
    const next = !open;
    setOpen(next);
    if (next) setTimeout(() => taRef.current?.focus(), 0);
  };

  return (
    <div>
      <button
        onClick={handleToggle}
        className={`inline-flex items-center gap-1 text-[10px] font-mono uppercase tracking-wider transition-colors ${
          hasNote ? "text-amber-400 hover:text-amber-300" : "text-stone-500 hover:text-stone-300"
        }`}
        title={hasNote ? "Edit note" : "Add note"}
      >
        <StickyNote className="w-3 h-3" />
        {hasNote ? "note" : "add note"}
      </button>

      {open && (
        <div className="mt-2">
          {/* key={jobId} remounts textarea on job change — avoids controlled-draft sync via effect */}
          <textarea
            ref={taRef}
            key={jobId}
            defaultValue={text}
            onBlur={(e) => setNote(jobId, e.target.value)}
            placeholder="Notes about this role…"
            rows={2}
            className="note-area w-full bg-stone-800/60 border border-stone-700 rounded px-3 py-2 text-xs font-mono text-stone-300 placeholder-stone-600 resize-none transition-colors"
          />
        </div>
      )}
    </div>
  );
}

// ─── Job Card ────────────────────────────────────────────────────────────────

function JobCard({ job, applied, onToggle, notes, setNote }) {
  const isApplied = !!applied[job.id];
  const isLive    = job.verification?.status === "verified_live";
  const accent    = isLive ? "teal" : job.is_static ? "sky" : "amber";

  const border = {
    teal:  "border-teal-600/30  hover:border-teal-500/60",
    sky:   "border-sky-600/25   hover:border-sky-500/50",
    amber: "border-amber-600/25 hover:border-amber-500/50",
  }[accent];

  const applyBtn = {
    teal:  "bg-teal-600  hover:bg-teal-500  text-stone-950",
    sky:   "bg-sky-600   hover:bg-sky-500   text-stone-950",
    amber: "bg-amber-600 hover:bg-amber-500 text-stone-950",
  }[accent];

  return (
    <article
      className={`job-card bg-stone-900/70 border rounded-lg overflow-hidden ${border} ${isApplied ? "opacity-55" : ""} animate-fade-in-up`}
    >
      {/* Card header */}
      <div className="flex items-center justify-between px-4 py-2 bg-stone-950/70 border-b border-stone-800">
        <StatusPill status={job.verification?.status} />
        <div className="flex items-center gap-1.5">
          {job.first_seen_at_run && <NewBadge />}
          {job.gpen_explicit      && <TierABadge certs={job.tier_a_certs} />}
        </div>
      </div>

      <div className="p-5">
        {/* Title + company */}
        <h3
          className="text-[15px] font-semibold text-stone-100 mb-0.5 leading-snug"
          style={{ fontFamily: '"Bricolage Grotesque", sans-serif' }}
        >
          {job.title}
        </h3>
        <div className="text-[11px] font-mono text-stone-500 mb-3">{job.company_name}</div>

        {/* Meta */}
        <div className="grid gap-1 text-[11px] font-mono mb-3 pb-3 border-b border-stone-800/60">
          {job.location && (
            <div className="flex items-start gap-1.5">
              <MapPin className="w-3 h-3 mt-0.5 shrink-0 text-stone-500" />
              <span className="text-stone-300">{job.location}</span>
            </div>
          )}
          {job.certs_mentioned?.length > 0 && (
            <div className="flex items-start gap-1.5">
              <Award className="w-3 h-3 mt-0.5 shrink-0 text-stone-500" />
              <span className="text-stone-300">{job.certs_mentioned.join(", ")}</span>
            </div>
          )}
          {job.last_seen && (
            <div className="flex items-center gap-1.5 text-stone-600">
              <Clock className="w-3 h-3 shrink-0" />
              <span>seen {relativeDate(job.last_seen)}</span>
            </div>
          )}
        </div>

        {/* URL */}
        <div className="flex items-center justify-between gap-2 mb-3 text-[10px] font-mono text-stone-600">
          <span className="truncate">{urlDomain(job.url)}</span>
          {job.url && <CopyButton text={job.url} />}
        </div>

        {/* Note */}
        <div className="mb-3">
          <JobNote jobId={job.id} notes={notes} setNote={setNote} />
        </div>

        {/* Actions */}
        <div className="flex flex-col sm:flex-row gap-2">
          <a
            href={job.apply_url || job.url || "#"}
            target="_blank"
            rel="noopener noreferrer"
            className={`flex-1 inline-flex items-center justify-center gap-1.5 px-4 py-2 rounded font-mono text-[11px] uppercase tracking-wider font-semibold transition-all ${applyBtn}`}
          >
            <ExternalLink className="w-3.5 h-3.5" />apply
          </a>
          <button
            onClick={() => onToggle(job.id)}
            className={`px-4 py-2 rounded font-mono text-[11px] uppercase tracking-wider transition-all border ${
              isApplied
                ? "bg-stone-800 border-stone-700 text-stone-300"
                : "bg-transparent border-stone-700 text-stone-400 hover:border-stone-500 hover:text-stone-300"
            }`}
          >
            {isApplied
              ? <span className="inline-flex items-center gap-1.5"><Check className="w-3.5 h-3.5" />applied</span>
              : <span className="inline-flex items-center gap-1.5"><Bookmark className="w-3.5 h-3.5" />mark applied</span>}
          </button>
        </div>
      </div>
    </article>
  );
}

// ─── Section (collapsible) ───────────────────────────────────────────────────

function Section({ id, label, color, count, children, defaultCollapsed = false }) {
  const [collapsed, toggleCollapsed] = useCollapsed(id, defaultCollapsed);

  const headText = COLOR_TEXT[color] ?? "text-stone-400";
  const borderText = {
    teal: "border-teal-500/30", amber: "border-amber-500/30", sky: "border-sky-500/30",
    rose: "border-rose-500/30", emerald: "border-emerald-500/30",
  }[color] ?? "border-stone-500/30";

  return (
    <section className="mb-10">
      <button
        onClick={toggleCollapsed}
        className={`w-full pb-2.5 border-b ${borderText} flex items-center justify-between`}
        aria-expanded={!collapsed}
      >
        <span className={`font-mono text-[11px] uppercase tracking-[0.25em] ${headText}`}>
          {label}
        </span>
        <div className="flex items-center gap-2">
          <span className="font-mono text-[11px] text-stone-500 tabular-nums">{count}</span>
          <ChevronDown
            className={`w-3.5 h-3.5 text-stone-500 transition-transform duration-200 ${collapsed ? "-rotate-90" : ""}`}
          />
        </div>
      </button>

      {!collapsed && (
        <div className="mt-4">
          {count === 0 ? (
            <div className="text-[11px] font-mono text-stone-600 italic">none</div>
          ) : (
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">{children}</div>
          )}
        </div>
      )}
    </section>
  );
}

// ─── Search bar ──────────────────────────────────────────────────────────────

function SearchBar({ query, setQuery }) {
  const ref = useRef(null);

  useEffect(() => {
    const handler = (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "k") {
        e.preventDefault();
        ref.current?.focus();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);

  return (
    <div className="relative">
      <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-stone-500 pointer-events-none" />
      <input
        ref={ref}
        type="text"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder="Search title, company, location, cert…"
        className="w-full bg-stone-900/70 border border-stone-700 rounded px-9 py-2 text-[12px] font-mono text-stone-300 placeholder-stone-600 focus:outline-none focus:border-stone-500 focus:ring-1 focus:ring-stone-600 transition-all"
        aria-label="Search jobs"
      />
      {query && (
        <button
          onClick={() => setQuery("")}
          className="absolute right-3 top-1/2 -translate-y-1/2 text-stone-500 hover:text-stone-300 transition-colors"
          aria-label="Clear search"
        >
          <X className="w-3.5 h-3.5" />
        </button>
      )}
    </div>
  );
}

// ─── Filter chips ────────────────────────────────────────────────────────────

const FILTERS = [
  { id: "all",     label: "All"     },
  { id: "new",     label: "New"     },
  { id: "tierA",   label: "Tier A"  },
  { id: "tierB",   label: "Tier B"  },
  { id: "static",  label: "Static"  },
  { id: "applied", label: "Applied" },
];

function FilterChips({ active, setActive, counts }) {
  return (
    <div className="flex flex-wrap gap-1.5">
      {FILTERS.map((f) => (
        <button
          key={f.id}
          onClick={() => setActive(f.id)}
          className={`px-2.5 py-1 rounded font-mono text-[10px] uppercase tracking-wider border transition-all ${
            active === f.id
              ? "bg-stone-700 border-stone-500 text-stone-100"
              : "bg-transparent border-stone-800 text-stone-500 hover:border-stone-600 hover:text-stone-300"
          }`}
        >
          {f.label}
          {counts[f.id] != null && (
            <span className={`ml-1.5 tabular-nums ${active === f.id ? "text-stone-300" : "text-stone-600"}`}>
              {counts[f.id]}
            </span>
          )}
        </button>
      ))}
    </div>
  );
}

// ─── Auto-refresh toggle ─────────────────────────────────────────────────────

function AutoRefreshToggle({ enabled, setEnabled }) {
  return (
    <button
      onClick={() => setEnabled((v) => !v)}
      className={`inline-flex items-center gap-1.5 text-[10px] font-mono uppercase tracking-wider transition-colors ${
        enabled ? "text-teal-400 hover:text-teal-300" : "text-stone-500 hover:text-stone-300"
      }`}
      title={enabled ? "Disable 60s auto-refresh" : "Enable 60s auto-refresh"}
    >
      <Timer className="w-3 h-3" />
      {enabled ? "auto · 60s" : "manual"}
    </button>
  );
}

// ─── Stat box ────────────────────────────────────────────────────────────────

function StatBox({ icon, color, label, value }) {
  const Icon = icon;
  const border = {
    emerald: "border-emerald-500/20", teal: "border-teal-500/20", lime: "border-lime-400/20",
    sky: "border-sky-500/20", rose: "border-rose-500/20", amber: "border-amber-500/20",
  }[color] ?? "border-stone-500/20";
  const text = COLOR_TEXT[color] ?? "text-stone-400";

  return (
    <div className={`bg-stone-900/60 border rounded px-3.5 py-3 ${border}`}>
      <div className="flex items-center gap-1.5 mb-1">
        <Icon className={`w-3 h-3 ${text}`} />
        <span className="text-[9px] uppercase tracking-widest text-stone-500">{label}</span>
      </div>
      <div className={`text-xl tabular-nums font-semibold ${text}`}>{value ?? 0}</div>
    </div>
  );
}

// ─── Main ────────────────────────────────────────────────────────────────────

export default function PriorityBoard() {
  const [data,        setData]        = useState(null);
  const [error,       setError]       = useState(null);
  const [loading,     setLoading]     = useState(false);
  const [lastRefresh, setLastRefresh] = useState(null);
  const [applied,     toggleApplied]  = useApplied();
  const [notes,       setNote]        = useNotes();
  const [query,       setQuery]       = useState("");
  const [activeFilter, setActiveFilter] = useState("all");
  const [hideApplied, setHideApplied] = useState(false);

  const loadSnapshot = useCallback(async () => {
    setLoading(true);
    try {
      const r = await fetch(`./jobs.json?t=${Date.now()}`, { cache: "no-store" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const json = await r.json();
      setData(json);
      setError(null);
      setLastRefresh(new Date());
    } catch (e) {
      setError(e.message);
    } finally {
      setTimeout(() => setLoading(false), 400);
    }
  }, []);

  const [autoEnabled, setAutoEnabled] = useAutoRefresh(loadSnapshot);

  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { loadSnapshot(); }, [loadSnapshot]);

  // Build filtered job list before bucketing
  const filteredJobs = useMemo(() => {
    if (!data?.jobs) return [];
    let jobs = data.jobs;

    // Skip when the Applied filter itself is active — otherwise hideApplied
    // would strip every job before the "applied" predicate ever runs,
    // making that tab always render empty.
    if (hideApplied && activeFilter !== "applied") jobs = jobs.filter((j) => !applied[j.id]);

    const pred = FILTER_PREDICATES[activeFilter];
    if (pred) jobs = jobs.filter((j) => pred(j, applied));

    if (query.trim()) {
      const q = query.toLowerCase();
      jobs = jobs.filter((j) => matchesQuery(j, q));
    }

    return jobs;
  }, [data, applied, hideApplied, activeFilter, query]);

  // Bucket filtered jobs into tiers. closedStatuses comes from the backend
  // (data.stats.closed_statuses) so this can't silently drift from what
  // output.py counts as "closed" — falls back to the same set for safety
  // if an older jobs.json without the field is loaded.
  const buckets = useMemo(() => {
    const closedStatuses = data?.stats?.closed_statuses ?? [
      "closed_404", "closed_410", "closed_inactive_text",
      "disappeared_from_api", "error",
    ];
    const empty = { tierA: [], tierB: [], tierC: [], closed: [] };
    return filteredJobs.reduce((acc, j) => {
      const s = j.verification?.status;
      const isClosed = closedStatuses.includes(s);
      if (isClosed)                                      acc.closed.push(j);
      else if (j.is_static)                              acc.tierC.push(j);
      else if (s === "verified_live" && j.gpen_explicit) acc.tierA.push(j);
      else if (s === "verified_live")                    acc.tierB.push(j);
      else                                               acc.tierC.push(j);
      return acc;
    }, empty);
  }, [filteredJobs, data]);

  // Counts for filter chip badges
  const chipCounts = useMemo(() => {
    if (!data?.jobs) return {};
    const baseJobs = hideApplied ? data.jobs.filter((j) => !applied[j.id]) : data.jobs;
    const withQuery = (jobs) => {
      if (!query.trim()) return jobs;
      const q = query.toLowerCase();
      return jobs.filter((j) => matchesQuery(j, q));
    };
    const counts = { all: withQuery(baseJobs).length };
    for (const [key, pred] of Object.entries(FILTER_PREDICATES)) {
      // "applied" counts against the full job list, not baseJobs — otherwise
      // hideApplied would zero out its own chip's count.
      const source = key === "applied" ? data.jobs : baseJobs;
      counts[key] = withQuery(source.filter((j) => pred(j, applied))).length;
    }
    return counts;
  }, [data, applied, hideApplied, query]);

  const companyCount = useMemo(
    () => (data ? new Set(data.jobs.map((j) => j.company_id)).size : 0),
    [data]
  );

  const appliedCount = Object.keys(applied).length;
  const isFiltered   = query || activeFilter !== "all" || hideApplied;

  return (
    <div className="min-h-screen bg-stone-950 text-stone-100 selection:bg-teal-500 selection:text-stone-950">
      <div
        className="relative max-w-5xl mx-auto px-4 sm:px-6 py-10 sm:py-14"
        style={{ fontFamily: '"IBM Plex Mono", monospace' }}
      >
        {/* ── Header ── */}
        <header className="mb-8">
          <div className="flex items-center gap-2 mb-3 font-mono text-[10px] uppercase tracking-[0.3em] text-stone-500">
            <Activity className="w-3 h-3 text-teal-400" />
            <span>recruit recon · live snapshot</span>
          </div>

          <div className="flex flex-col sm:flex-row sm:items-end sm:justify-between gap-3 mb-5">
            <h1
              className="text-3xl sm:text-4xl text-stone-100 leading-tight tracking-tight"
              style={{ fontFamily: '"Bricolage Grotesque", sans-serif' }}
            >
              Cyber Job Board
            </h1>
            <div className="flex items-center gap-4 font-mono text-[10px] text-stone-500 uppercase tracking-widest">
              {data && (
                <span>
                  <span className="text-stone-400">
                    {companyCount}
                  </span>{" "}companies
                </span>
              )}
              {appliedCount > 0 && (
                <span>
                  <span className="text-teal-400 font-semibold">{appliedCount}</span> applied
                </span>
              )}
            </div>
          </div>

          {error && (
            <div className="bg-rose-500/10 border border-rose-500/25 rounded px-4 py-3 mb-4">
              <div className="flex items-start gap-2">
                <XCircle className="w-4 h-4 text-rose-400 mt-0.5 shrink-0" />
                <div>
                  <div className="text-sm text-rose-200">Failed to load jobs.json</div>
                  <div className="text-[11px] text-rose-300/70 font-mono mt-1">{error}</div>
                  <div className="text-[11px] text-stone-400 mt-2">
                    Run <code className="text-teal-400">python recon.py</code> to generate it.
                  </div>
                </div>
              </div>
            </div>
          )}

          {data && (
            <>
              {/* Meta row */}
              <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-[10px] font-mono text-stone-500 mb-5">
                <span className="inline-flex items-center gap-1.5">
                  <Database className="w-3 h-3" />
                  generated {new Date(data.generated_at).toLocaleString()}
                </span>
                <button
                  onClick={loadSnapshot}
                  disabled={loading}
                  className="inline-flex items-center gap-1.5 text-stone-400 hover:text-teal-400 disabled:opacity-40 transition-colors"
                >
                  <RefreshCw className={`w-3 h-3 ${loading ? "animate-spin" : ""}`} />
                  {loading ? "refreshing…" : "refresh"}
                </button>
                {lastRefresh && !loading && (
                  <span className="text-teal-400/50">✓ {lastRefresh.toLocaleTimeString()}</span>
                )}
                <AutoRefreshToggle enabled={autoEnabled} setEnabled={setAutoEnabled} />
              </div>

              {/* Stat boxes */}
              <div className="grid grid-cols-2 sm:grid-cols-5 gap-2.5 mb-6">
                <StatBox icon={CheckCircle2} color="emerald" label="verified live" value={data.stats.verified_live} />
                <StatBox icon={Award}        color="teal"    label="cert match"    value={data.stats.gpen_match} />
                <StatBox icon={Clock}        color="sky"     label="static"        value={data.stats.static} />
                <StatBox icon={XCircle}      color="rose"    label="closed"        value={data.stats.closed} />
                <StatBox icon={Sparkles}     color="lime"    label="new this run"  value={data.stats.new_jobs_this_run} />
              </div>
            </>
          )}
        </header>

        {data && (
          <>
            {/* ── Controls ── */}
            <div className="mb-6 space-y-3">
              <SearchBar query={query} setQuery={setQuery} />
              <div className="flex flex-wrap items-center justify-between gap-3">
                <FilterChips active={activeFilter} setActive={setActiveFilter} counts={chipCounts} />
                <button
                  onClick={() => setHideApplied((v) => !v)}
                  className={`inline-flex items-center gap-1.5 text-[10px] font-mono uppercase tracking-wider transition-colors ${
                    hideApplied ? "text-amber-400 hover:text-amber-300" : "text-stone-500 hover:text-stone-300"
                  }`}
                >
                  {hideApplied ? <EyeOff className="w-3 h-3" /> : <Eye className="w-3 h-3" />}
                  {hideApplied ? "hide applied" : "show all"}
                </button>
              </div>
              {isFiltered && (
                <div className="text-[10px] font-mono text-stone-600">
                  showing <span className="text-stone-400">{filteredJobs.length}</span> of{" "}
                  <span className="text-stone-400">{data.jobs.length}</span> jobs
                </div>
              )}
            </div>

            {/* ── Tier sections ── */}
            <Section id="tierA" label="tier a · verified live + cert match" color="teal"  count={buckets.tierA.length}>
              {buckets.tierA.map((j) => <JobCard key={j.id} job={j} applied={applied} onToggle={toggleApplied} notes={notes} setNote={setNote} />)}
            </Section>

            <Section id="tierB" label="tier b · verified live"              color="amber" count={buckets.tierB.length}>
              {buckets.tierB.map((j) => <JobCard key={j.id} job={j} applied={applied} onToggle={toggleApplied} notes={notes} setNote={setNote} />)}
            </Section>

            <Section id="tierC" label="tier c · static / unverified"        color="sky"   count={buckets.tierC.length}>
              {buckets.tierC.map((j) => <JobCard key={j.id} job={j} applied={applied} onToggle={toggleApplied} notes={notes} setNote={setNote} />)}
            </Section>

            <Section id="closed" label="closed / disappeared" color="rose" count={buckets.closed.length} defaultCollapsed>
              {buckets.closed.map((j) => <JobCard key={j.id} job={j} applied={applied} onToggle={toggleApplied} notes={notes} setNote={setNote} />)}
            </Section>
          </>
        )}

        <footer className="mt-10 pt-5 border-t border-stone-800/60 flex flex-wrap items-center justify-between gap-2 text-[9px] font-mono text-stone-700 uppercase tracking-widest">
          <span>recruit recon · self-hosted · state in localStorage</span>
          <span>⌘K / Ctrl+K to search</span>
        </footer>
      </div>
    </div>
  );
}
