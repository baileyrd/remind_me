import { useState, useEffect, useCallback, useRef } from "react";

// -- Theme & Design Tokens --
const theme = {
  bg: "#0a0a0f",
  surface: "#12121a",
  surfaceHover: "#1a1a26",
  surfaceActive: "#22222e",
  border: "#2a2a3a",
  borderFocus: "#6366f1",
  text: "#e4e4ed",
  textSecondary: "#8888a4",
  textMuted: "#55556a",
  accent: "#6366f1",
  accentHover: "#818cf8",
  accentSubtle: "rgba(99,102,241,0.12)",
  danger: "#ef4444",
  dangerSubtle: "rgba(239,68,68,0.12)",
  success: "#22c55e",
  successSubtle: "rgba(34,197,94,0.12)",
  warning: "#f59e0b",
  warningSubtle: "rgba(245,158,11,0.12)",
  categoryColors: {
    general: "#6366f1",
    preference: "#f59e0b",
    fact: "#22c55e",
    project: "#06b6d4",
    person: "#ec4899",
    decision: "#8b5cf6",
    chat_import: "#64748b",
    observation: "#14b8a6",
  },
};

const fontStack = `'IBM Plex Mono', 'JetBrains Mono', 'Fira Code', monospace`;
const sansStack = `'IBM Plex Sans', -apple-system, BlinkMacSystemFont, sans-serif`;

// -- Mock Data Layer --
// This simulates the MCP server responses for the standalone UI.
// In production, replace with actual MCP tool calls or a REST API wrapper.

const generateId = () => Math.random().toString(36).substring(2, 14);
const now = () => new Date().toISOString();

const INITIAL_MEMORIES = [
  { id: "64c309c735fc", content: "Nano prefers Python with type hints and async-first patterns. Always use Pydantic for validation and prefer composition over inheritance.", category: "preference", tags: ["python", "coding-style"], source: "manual", metadata: {}, created_at: "2026-02-20T10:00:00Z", updated_at: "2026-02-20T10:00:00Z" },
  { id: "e1a4fd005625", content: "The DTO manages a 398-application portfolio supporting 3,000+ personnel across 8+ PM offices, with critical initiatives including Microsoft Project Online migration by September 2026.", category: "fact", tags: ["work", "dto", "portfolio"], source: "manual", metadata: {}, created_at: "2026-02-19T14:30:00Z", updated_at: "2026-02-19T14:30:00Z" },
  { id: "3aea16b3832c", content: "UV is the preferred Python package manager for new projects. Use `uv pip install` for speed and `uv run` for script execution.", category: "preference", tags: ["python", "tooling", "uv"], source: "manual", metadata: {}, created_at: "2026-02-18T09:15:00Z", updated_at: "2026-02-18T09:15:00Z" },
  { id: "0efa92a8fbad", content: "To set up VLANs on your home network, you'll need a managed switch. Configure trunk ports between your router and switch, then assign access ports to specific VLAN IDs.", category: "chat_import", tags: ["networking", "homelab"], source: "chat_import", metadata: { filename: "claude_chat.json", import_id: "37198455d751" }, created_at: "2026-02-17T16:00:00Z", updated_at: "2026-02-17T16:00:00Z" },
  { id: "8b13f3ac4317", content: "DuckDB is an in-process analytical database that excels at OLAP queries on local data. It can directly query Parquet, CSV, and JSON files without loading them into a table first.", category: "chat_import", tags: ["data-engineering", "databases"], source: "chat_import", metadata: { filename: "chat.md", import_id: "9fd242af42a9" }, created_at: "2026-02-16T11:20:00Z", updated_at: "2026-02-16T11:20:00Z" },
  { id: "1861e297d021", content: "Rust uses the Result<T, E> type for recoverable errors and panic! for unrecoverable ones. Use the ? operator to propagate errors, and define custom error types with thiserror or anyhow.", category: "chat_import", tags: ["rust", "error-handling"], source: "chat_import", metadata: { filename: "openai_chat.json", import_id: "9ed71c913c87" }, created_at: "2026-02-15T08:45:00Z", updated_at: "2026-02-15T08:45:00Z" },
  { id: "a7b3c1d2e3f4", content: "Design principles: Architecture — favor composition over inheritance, prefer microservices over monoliths. Code style — explicit over implicit, minimal dependencies. Zen principles — beautiful over ugly, simple over complex.", category: "preference", tags: ["architecture", "principles"], source: "manual", metadata: {}, created_at: "2026-02-14T13:00:00Z", updated_at: "2026-02-14T13:00:00Z" },
  { id: "f5e6d7c8b9a0", content: "Claude Code MCP config lives at ~/.claude/claude_code_config.json. Claude Desktop config is at ~/Library/Application Support/Claude/claude_desktop_config.json on macOS.", category: "fact", tags: ["claude", "configuration", "mcp"], source: "manual", metadata: {}, created_at: "2026-02-13T10:30:00Z", updated_at: "2026-02-13T10:30:00Z" },
  { id: "b2c3d4e5f6a7", content: "The DTO has three functional areas: Digital Enterprise (DEFAs), Knowledge Management, and Application Development, with approximately 23 FTE across these areas.", category: "fact", tags: ["work", "dto", "organization"], source: "manual", metadata: {}, created_at: "2026-02-12T15:45:00Z", updated_at: "2026-02-12T15:45:00Z" },
  { id: "c8d9e0f1a2b3", content: "For home lab networking: use pfSense or OPNsense for firewall/routing, Proxmox for virtualization, and Syncthing for cross-machine file sync without cloud dependency.", category: "decision", tags: ["homelab", "infrastructure"], source: "manual", metadata: {}, created_at: "2026-02-11T09:00:00Z", updated_at: "2026-02-11T09:00:00Z" },
];

function useMemoryStore() {
  const [memories, setMemories] = useState(INITIAL_MEMORIES);

  const search = useCallback((query, category, tags) => {
    const q = query.toLowerCase();
    return memories.filter((m) => {
      const textMatch = !query || m.content.toLowerCase().includes(q) || m.tags.some((t) => t.toLowerCase().includes(q)) || m.category.toLowerCase().includes(q);
      const catMatch = !category || m.category === category;
      const tagMatch = !tags || tags.length === 0 || tags.every((t) => m.tags.includes(t));
      return textMatch && catMatch && tagMatch;
    });
  }, [memories]);

  const add = useCallback((memory) => {
    const newMem = { id: generateId(), created_at: now(), updated_at: now(), source: "manual", metadata: {}, ...memory };
    setMemories((prev) => [newMem, ...prev]);
    return newMem;
  }, []);

  const update = useCallback((id, updates) => {
    setMemories((prev) => prev.map((m) => m.id === id ? { ...m, ...updates, updated_at: now() } : m));
  }, []);

  const remove = useCallback((id) => {
    setMemories((prev) => prev.filter((m) => m.id !== id));
  }, []);

  const stats = useCallback(() => {
    const categories = {};
    const sources = {};
    const allTags = {};
    memories.forEach((m) => {
      categories[m.category] = (categories[m.category] || 0) + 1;
      sources[m.source] = (sources[m.source] || 0) + 1;
      m.tags.forEach((t) => { allTags[t] = (allTags[t] || 0) + 1; });
    });
    return { total: memories.length, categories, sources, tags: allTags };
  }, [memories]);

  return { memories, search, add, update, remove, stats };
}

// -- Icon Components --
const Icons = {
  Search: () => <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>,
  Plus: () => <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M12 5v14M5 12h14"/></svg>,
  Trash: () => <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>,
  Edit: () => <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>,
  Brain: () => <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"><path d="M12 2a7 7 0 0 0-7 7c0 3 2 5.5 4 7l3 3 3-3c2-1.5 4-4 4-7a7 7 0 0 0-7-7z"/><path d="M12 2v10"/><path d="M8 6c1.5 1 3 1.5 4 1.5s2.5-.5 4-1.5"/></svg>,
  Chart: () => <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M18 20V10M12 20V4M6 20v-6"/></svg>,
  Upload: () => <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>,
  X: () => <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>,
  Copy: () => <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>,
  Tag: () => <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"/><line x1="7" y1="7" x2="7.01" y2="7"/></svg>,
  Check: () => <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"><polyline points="20 6 9 17 4 12"/></svg>,
  Database: () => <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>,
};

// -- Subcomponents --

function CategoryBadge({ category }) {
  const color = theme.categoryColors[category] || theme.accent;
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 4,
      padding: "2px 8px", borderRadius: 4, fontSize: 11, fontWeight: 600,
      fontFamily: fontStack, letterSpacing: "0.04em", textTransform: "uppercase",
      background: `${color}18`, color: color, border: `1px solid ${color}30`,
    }}>
      {category}
    </span>
  );
}

function TagPill({ tag, onClick, removable, onRemove }) {
  return (
    <span
      onClick={onClick}
      style={{
        display: "inline-flex", alignItems: "center", gap: 3,
        padding: "1px 7px", borderRadius: 3, fontSize: 11,
        fontFamily: fontStack, background: theme.surfaceActive,
        color: theme.textSecondary, border: `1px solid ${theme.border}`,
        cursor: onClick ? "pointer" : "default",
        transition: "all 0.15s",
      }}
      onMouseEnter={(e) => { if (onClick) { e.currentTarget.style.borderColor = theme.accent; e.currentTarget.style.color = theme.text; } }}
      onMouseLeave={(e) => { if (onClick) { e.currentTarget.style.borderColor = theme.border; e.currentTarget.style.color = theme.textSecondary; } }}
    >
      <Icons.Tag />{tag}
      {removable && onRemove && (
        <span onClick={(e) => { e.stopPropagation(); onRemove(); }} style={{ cursor: "pointer", marginLeft: 2, opacity: 0.6 }}>×</span>
      )}
    </span>
  );
}

function MemoryCard({ memory, onEdit, onDelete, onTagClick, expanded, onToggle }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = () => {
    navigator.clipboard.writeText(memory.content);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  const isLong = memory.content.length > 200;
  const displayContent = expanded || !isLong ? memory.content : memory.content.slice(0, 200) + "…";
  const metaEntries = Object.entries(memory.metadata || {}).filter(([k]) => k !== "import_id");

  return (
    <div style={{
      background: theme.surface, border: `1px solid ${theme.border}`,
      borderRadius: 8, padding: "16px 18px", transition: "all 0.2s",
      position: "relative",
    }}
      onMouseEnter={(e) => { e.currentTarget.style.borderColor = theme.borderFocus + "60"; e.currentTarget.style.background = theme.surfaceHover; }}
      onMouseLeave={(e) => { e.currentTarget.style.borderColor = theme.border; e.currentTarget.style.background = theme.surface; }}
    >
      {/* Header */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 8 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <CategoryBadge category={memory.category} />
          <code style={{ fontSize: 11, color: theme.textMuted, fontFamily: fontStack }}>{memory.id}</code>
          {memory.source === "chat_import" && memory.metadata?.filename && (
            <span style={{ fontSize: 11, color: theme.textMuted, fontFamily: fontStack }}>← {memory.metadata.filename}</span>
          )}
        </div>
        <div style={{ display: "flex", gap: 4 }}>
          <button onClick={handleCopy} title="Copy" style={iconBtnStyle}>
            {copied ? <Icons.Check /> : <Icons.Copy />}
          </button>
          <button onClick={() => onEdit(memory)} title="Edit" style={iconBtnStyle}><Icons.Edit /></button>
          <button onClick={() => onDelete(memory.id)} title="Delete" style={{ ...iconBtnStyle, color: theme.danger }}><Icons.Trash /></button>
        </div>
      </div>

      {/* Content */}
      <div
        onClick={isLong ? onToggle : undefined}
        style={{
          fontFamily: sansStack, fontSize: 14, lineHeight: 1.65,
          color: theme.text, whiteSpace: "pre-wrap", wordBreak: "break-word",
          cursor: isLong ? "pointer" : "default",
        }}
      >
        {displayContent}
      </div>
      {isLong && !expanded && (
        <button onClick={onToggle} style={{ background: "none", border: "none", color: theme.accent, fontSize: 12, fontFamily: fontStack, cursor: "pointer", padding: "4px 0", marginTop: 4 }}>
          Show more
        </button>
      )}
      {expanded && isLong && (
        <button onClick={onToggle} style={{ background: "none", border: "none", color: theme.accent, fontSize: 12, fontFamily: fontStack, cursor: "pointer", padding: "4px 0", marginTop: 4 }}>
          Show less
        </button>
      )}

      {/* Tags & Meta */}
      <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginTop: 10 }}>
        {memory.tags.map((t) => <TagPill key={t} tag={t} onClick={() => onTagClick(t)} />)}
      </div>
      {metaEntries.length > 0 && (
        <div style={{ marginTop: 6, fontSize: 11, color: theme.textMuted, fontFamily: fontStack }}>
          {metaEntries.map(([k, v]) => `${k}: ${v}`).join(" · ")}
        </div>
      )}
      <div style={{ marginTop: 8, fontSize: 11, color: theme.textMuted, fontFamily: fontStack }}>
        {new Date(memory.created_at).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })}
        {memory.updated_at !== memory.created_at && ` · edited ${new Date(memory.updated_at).toLocaleDateString("en-US", { month: "short", day: "numeric" })}`}
      </div>
    </div>
  );
}

const iconBtnStyle = {
  background: "none", border: "none", color: theme.textSecondary,
  cursor: "pointer", padding: 4, borderRadius: 4, display: "flex",
  alignItems: "center", transition: "color 0.15s",
};

function StatCard({ label, value, color, icon }) {
  return (
    <div style={{
      background: theme.surface, border: `1px solid ${theme.border}`,
      borderRadius: 8, padding: "14px 16px", flex: "1 1 140px", minWidth: 140,
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 6 }}>
        <span style={{ color: color || theme.accent }}>{icon}</span>
        <span style={{ fontSize: 11, color: theme.textMuted, fontFamily: fontStack, textTransform: "uppercase", letterSpacing: "0.06em" }}>{label}</span>
      </div>
      <div style={{ fontSize: 28, fontWeight: 700, fontFamily: fontStack, color: theme.text, lineHeight: 1 }}>{value}</div>
    </div>
  );
}

function BarChart({ data, colorMap }) {
  const max = Math.max(...Object.values(data), 1);
  const entries = Object.entries(data).sort((a, b) => b[1] - a[1]);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      {entries.map(([label, count]) => (
        <div key={label} style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ width: 90, fontSize: 12, fontFamily: fontStack, color: theme.textSecondary, textAlign: "right", flexShrink: 0 }}>{label}</span>
          <div style={{ flex: 1, height: 20, background: theme.surfaceActive, borderRadius: 3, overflow: "hidden" }}>
            <div style={{
              height: "100%", width: `${(count / max) * 100}%`,
              background: colorMap?.[label] || theme.accent,
              borderRadius: 3, transition: "width 0.4s ease",
              display: "flex", alignItems: "center", justifyContent: "flex-end", paddingRight: 6,
            }}>
              <span style={{ fontSize: 10, fontWeight: 700, fontFamily: fontStack, color: "#fff" }}>{count}</span>
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

// -- Modal --
function Modal({ open, onClose, title, children, width }) {
  if (!open) return null;
  return (
    <div style={{
      position: "fixed", inset: 0, zIndex: 1000,
      display: "flex", alignItems: "center", justifyContent: "center",
      background: "rgba(0,0,0,0.65)", backdropFilter: "blur(4px)",
    }} onClick={onClose}>
      <div onClick={(e) => e.stopPropagation()} style={{
        background: theme.surface, border: `1px solid ${theme.border}`,
        borderRadius: 12, padding: 24, width: width || 480, maxWidth: "92vw",
        maxHeight: "85vh", overflowY: "auto",
        boxShadow: "0 24px 80px rgba(0,0,0,0.5)",
      }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 20 }}>
          <h2 style={{ margin: 0, fontSize: 18, fontFamily: sansStack, fontWeight: 600, color: theme.text }}>{title}</h2>
          <button onClick={onClose} style={{ ...iconBtnStyle, color: theme.textMuted }}><Icons.X /></button>
        </div>
        {children}
      </div>
    </div>
  );
}

// -- Input styling helper --
const inputStyle = {
  width: "100%", padding: "10px 12px", borderRadius: 6,
  border: `1px solid ${theme.border}`, background: theme.bg,
  color: theme.text, fontSize: 14, fontFamily: sansStack,
  outline: "none", transition: "border-color 0.15s",
  boxSizing: "border-box",
};

const labelStyle = {
  display: "block", fontSize: 12, fontWeight: 600,
  fontFamily: fontStack, color: theme.textSecondary,
  marginBottom: 6, textTransform: "uppercase", letterSpacing: "0.04em",
};

// -- Add/Edit Form --
function MemoryForm({ initial, onSubmit, onCancel }) {
  const [content, setContent] = useState(initial?.content || "");
  const [category, setCategory] = useState(initial?.category || "general");
  const [tagInput, setTagInput] = useState("");
  const [tags, setTags] = useState(initial?.tags || []);

  const handleTagKey = (e) => {
    if ((e.key === "Enter" || e.key === ",") && tagInput.trim()) {
      e.preventDefault();
      const tag = tagInput.trim().toLowerCase().replace(/,/g, "");
      if (tag && !tags.includes(tag)) setTags([...tags, tag]);
      setTagInput("");
    }
  };

  const handleSubmit = () => {
    if (!content.trim()) return;
    onSubmit({ content: content.trim(), category, tags });
  };

  const categories = ["general", "preference", "fact", "project", "person", "decision", "observation"];

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <div>
        <label style={labelStyle}>Content</label>
        <textarea
          value={content} onChange={(e) => setContent(e.target.value)}
          rows={5} placeholder="What should I remember?"
          style={{ ...inputStyle, resize: "vertical", fontFamily: sansStack, lineHeight: 1.6 }}
          onFocus={(e) => e.target.style.borderColor = theme.borderFocus}
          onBlur={(e) => e.target.style.borderColor = theme.border}
        />
      </div>
      <div>
        <label style={labelStyle}>Category</label>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
          {categories.map((c) => (
            <button key={c} onClick={() => setCategory(c)} style={{
              padding: "6px 12px", borderRadius: 6, fontSize: 12,
              fontFamily: fontStack, border: `1px solid ${category === c ? (theme.categoryColors[c] || theme.accent) : theme.border}`,
              background: category === c ? `${theme.categoryColors[c] || theme.accent}18` : "transparent",
              color: category === c ? (theme.categoryColors[c] || theme.accent) : theme.textSecondary,
              cursor: "pointer", transition: "all 0.15s", textTransform: "uppercase",
              fontWeight: category === c ? 600 : 400,
            }}>
              {c}
            </button>
          ))}
        </div>
      </div>
      <div>
        <label style={labelStyle}>Tags</label>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginBottom: tags.length > 0 ? 8 : 0 }}>
          {tags.map((t) => <TagPill key={t} tag={t} removable onRemove={() => setTags(tags.filter((x) => x !== t))} />)}
        </div>
        <input
          value={tagInput} onChange={(e) => setTagInput(e.target.value)}
          onKeyDown={handleTagKey} placeholder="Type a tag and press Enter…"
          style={inputStyle}
          onFocus={(e) => e.target.style.borderColor = theme.borderFocus}
          onBlur={(e) => e.target.style.borderColor = theme.border}
        />
      </div>
      <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 8 }}>
        <button onClick={onCancel} style={{
          padding: "8px 16px", borderRadius: 6, border: `1px solid ${theme.border}`,
          background: "transparent", color: theme.textSecondary, fontSize: 13,
          fontFamily: fontStack, cursor: "pointer",
        }}>Cancel</button>
        <button onClick={handleSubmit} disabled={!content.trim()} style={{
          padding: "8px 20px", borderRadius: 6, border: "none",
          background: content.trim() ? theme.accent : theme.surfaceActive,
          color: content.trim() ? "#fff" : theme.textMuted, fontSize: 13, fontWeight: 600,
          fontFamily: fontStack, cursor: content.trim() ? "pointer" : "not-allowed",
        }}>
          {initial ? "Save Changes" : "Add Memory"}
        </button>
      </div>
    </div>
  );
}

// -- Main App --
export default function MemoryDashboard() {
  const store = useMemoryStore();
  const [view, setView] = useState("browse"); // browse | stats
  const [searchQuery, setSearchQuery] = useState("");
  const [filterCategory, setFilterCategory] = useState("");
  const [filterTags, setFilterTags] = useState([]);
  const [showAddModal, setShowAddModal] = useState(false);
  const [editMemory, setEditMemory] = useState(null);
  const [deleteConfirm, setDeleteConfirm] = useState(null);
  const [expandedIds, setExpandedIds] = useState(new Set());
  const searchRef = useRef(null);

  // Keyboard shortcut
  useEffect(() => {
    const handler = (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        searchRef.current?.focus();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);

  const filteredMemories = store.search(searchQuery, filterCategory || null, filterTags.length ? filterTags : null);
  const stats = store.stats();

  const handleAdd = (data) => {
    store.add(data);
    setShowAddModal(false);
  };

  const handleEdit = (data) => {
    if (editMemory) {
      store.update(editMemory.id, data);
      setEditMemory(null);
    }
  };

  const handleDelete = (id) => {
    store.remove(id);
    setDeleteConfirm(null);
  };

  const toggleExpand = (id) => {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  };

  const handleTagClick = (tag) => {
    if (!filterTags.includes(tag)) {
      setFilterTags([...filterTags, tag]);
    }
  };

  const allCategories = Object.keys(stats.categories);
  const allTags = Object.keys(stats.tags).sort((a, b) => stats.tags[b] - stats.tags[a]);

  return (
    <div style={{
      minHeight: "100vh", background: theme.bg, color: theme.text,
      fontFamily: sansStack,
    }}>
      {/* Global font import */}
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600;700&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap');
        * { box-sizing: border-box; }
        ::-webkit-scrollbar { width: 6px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: ${theme.border}; border-radius: 3px; }
        ::-webkit-scrollbar-thumb:hover { background: ${theme.textMuted}; }
        ::selection { background: ${theme.accent}40; }
      `}</style>

      {/* Header */}
      <header style={{
        borderBottom: `1px solid ${theme.border}`,
        padding: "16px 24px",
        display: "flex", alignItems: "center", justifyContent: "space-between",
        position: "sticky", top: 0, zIndex: 100,
        background: `${theme.bg}e6`, backdropFilter: "blur(12px)",
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <div style={{
            width: 36, height: 36, borderRadius: 8,
            background: `linear-gradient(135deg, ${theme.accent}, #a855f7)`,
            display: "flex", alignItems: "center", justifyContent: "center",
          }}>
            <Icons.Brain />
          </div>
          <div>
            <h1 style={{ margin: 0, fontSize: 18, fontWeight: 700, fontFamily: sansStack, letterSpacing: "-0.02em" }}>Memory</h1>
            <span style={{ fontSize: 11, color: theme.textMuted, fontFamily: fontStack }}>{stats.total} memories · ~/.remind-me</span>
          </div>
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          {/* View toggle */}
          <div style={{ display: "flex", background: theme.surface, borderRadius: 6, border: `1px solid ${theme.border}`, overflow: "hidden" }}>
            {[["browse", "Browse"], ["stats", "Stats"]].map(([v, label]) => (
              <button key={v} onClick={() => setView(v)} style={{
                padding: "6px 14px", border: "none", fontSize: 12,
                fontFamily: fontStack, fontWeight: 500, cursor: "pointer",
                background: view === v ? theme.accent : "transparent",
                color: view === v ? "#fff" : theme.textSecondary,
                transition: "all 0.15s",
              }}>{label}</button>
            ))}
          </div>
          <button onClick={() => setShowAddModal(true)} style={{
            display: "flex", alignItems: "center", gap: 6,
            padding: "8px 14px", borderRadius: 6, border: "none",
            background: theme.accent, color: "#fff", fontSize: 13,
            fontWeight: 600, fontFamily: fontStack, cursor: "pointer",
            transition: "background 0.15s",
          }}
            onMouseEnter={(e) => e.currentTarget.style.background = theme.accentHover}
            onMouseLeave={(e) => e.currentTarget.style.background = theme.accent}
          >
            <Icons.Plus /> Add
          </button>
        </div>
      </header>

      <div style={{ display: "flex", maxWidth: 1200, margin: "0 auto" }}>
        {/* Sidebar */}
        {view === "browse" && (
          <aside style={{
            width: 220, borderRight: `1px solid ${theme.border}`,
            padding: "20px 16px", flexShrink: 0,
            position: "sticky", top: 69, height: "calc(100vh - 69px)",
            overflowY: "auto",
          }}>
            <div style={{ marginBottom: 20 }}>
              <div style={{ ...labelStyle, marginBottom: 10 }}>Categories</div>
              <button onClick={() => setFilterCategory("")} style={{
                display: "block", width: "100%", textAlign: "left",
                padding: "6px 10px", borderRadius: 5, border: "none",
                background: !filterCategory ? theme.accentSubtle : "transparent",
                color: !filterCategory ? theme.accent : theme.textSecondary,
                fontSize: 13, fontFamily: sansStack, cursor: "pointer",
                fontWeight: !filterCategory ? 600 : 400, marginBottom: 2,
              }}>
                All ({stats.total})
              </button>
              {allCategories.map((cat) => (
                <button key={cat} onClick={() => setFilterCategory(filterCategory === cat ? "" : cat)} style={{
                  display: "flex", alignItems: "center", justifyContent: "space-between",
                  width: "100%", textAlign: "left",
                  padding: "6px 10px", borderRadius: 5, border: "none",
                  background: filterCategory === cat ? `${theme.categoryColors[cat] || theme.accent}18` : "transparent",
                  color: filterCategory === cat ? (theme.categoryColors[cat] || theme.accent) : theme.textSecondary,
                  fontSize: 13, fontFamily: sansStack, cursor: "pointer",
                  fontWeight: filterCategory === cat ? 600 : 400, marginBottom: 2,
                }}>
                  <span>{cat}</span>
                  <span style={{ fontSize: 11, fontFamily: fontStack, opacity: 0.7 }}>{stats.categories[cat]}</span>
                </button>
              ))}
            </div>

            <div>
              <div style={{ ...labelStyle, marginBottom: 10 }}>Popular Tags</div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
                {allTags.slice(0, 15).map((tag) => (
                  <TagPill
                    key={tag} tag={tag}
                    onClick={() => handleTagClick(tag)}
                  />
                ))}
              </div>
            </div>
          </aside>
        )}

        {/* Main Content */}
        <main style={{ flex: 1, padding: "20px 24px", minWidth: 0 }}>
          {view === "browse" ? (
            <>
              {/* Search bar */}
              <div style={{ position: "relative", marginBottom: 16 }}>
                <div style={{ position: "absolute", left: 12, top: "50%", transform: "translateY(-50%)", color: theme.textMuted }}>
                  <Icons.Search />
                </div>
                <input
                  ref={searchRef}
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  placeholder="Search memories… (⌘K)"
                  style={{
                    ...inputStyle, paddingLeft: 36, paddingRight: 12,
                    background: theme.surface, fontSize: 15,
                  }}
                  onFocus={(e) => e.target.style.borderColor = theme.borderFocus}
                  onBlur={(e) => e.target.style.borderColor = theme.border}
                />
              </div>

              {/* Active filters */}
              {filterTags.length > 0 && (
                <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 12, flexWrap: "wrap" }}>
                  <span style={{ fontSize: 12, color: theme.textMuted, fontFamily: fontStack }}>Filtered by:</span>
                  {filterTags.map((t) => (
                    <TagPill key={t} tag={t} removable onRemove={() => setFilterTags(filterTags.filter((x) => x !== t))} />
                  ))}
                  <button onClick={() => setFilterTags([])} style={{
                    background: "none", border: "none", color: theme.accent,
                    fontSize: 12, fontFamily: fontStack, cursor: "pointer",
                  }}>Clear all</button>
                </div>
              )}

              {/* Results count */}
              <div style={{ fontSize: 12, color: theme.textMuted, fontFamily: fontStack, marginBottom: 12 }}>
                {filteredMemories.length} {filteredMemories.length === 1 ? "memory" : "memories"}
                {(searchQuery || filterCategory || filterTags.length > 0) && " matching filters"}
              </div>

              {/* Memory list */}
              <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                {filteredMemories.map((m) => (
                  <MemoryCard
                    key={m.id} memory={m}
                    onEdit={setEditMemory}
                    onDelete={setDeleteConfirm}
                    onTagClick={handleTagClick}
                    expanded={expandedIds.has(m.id)}
                    onToggle={() => toggleExpand(m.id)}
                  />
                ))}
                {filteredMemories.length === 0 && (
                  <div style={{
                    textAlign: "center", padding: "60px 20px",
                    color: theme.textMuted, fontFamily: sansStack,
                  }}>
                    <div style={{ fontSize: 40, marginBottom: 12 }}>∅</div>
                    <div style={{ fontSize: 15, marginBottom: 6 }}>No memories found</div>
                    <div style={{ fontSize: 13 }}>Try adjusting your search or filters</div>
                  </div>
                )}
              </div>
            </>
          ) : (
            /* Stats View */
            <div>
              <h2 style={{ fontFamily: sansStack, fontWeight: 700, fontSize: 22, marginBottom: 20, letterSpacing: "-0.02em" }}>
                Memory Statistics
              </h2>

              {/* Stat cards */}
              <div style={{ display: "flex", gap: 12, marginBottom: 24, flexWrap: "wrap" }}>
                <StatCard label="Total Memories" value={stats.total} color={theme.accent} icon={<Icons.Database />} />
                <StatCard label="Categories" value={Object.keys(stats.categories).length} color="#22c55e" icon={<Icons.Chart />} />
                <StatCard label="Unique Tags" value={Object.keys(stats.tags).length} color="#f59e0b" icon={<Icons.Tag />} />
                <StatCard label="Sources" value={Object.keys(stats.sources).length} color="#06b6d4" icon={<Icons.Upload />} />
              </div>

              {/* Charts */}
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
                <div style={{ background: theme.surface, border: `1px solid ${theme.border}`, borderRadius: 8, padding: 20 }}>
                  <h3 style={{ fontFamily: fontStack, fontSize: 13, fontWeight: 600, color: theme.textSecondary, marginBottom: 16, textTransform: "uppercase", letterSpacing: "0.04em" }}>
                    By Category
                  </h3>
                  <BarChart data={stats.categories} colorMap={theme.categoryColors} />
                </div>
                <div style={{ background: theme.surface, border: `1px solid ${theme.border}`, borderRadius: 8, padding: 20 }}>
                  <h3 style={{ fontFamily: fontStack, fontSize: 13, fontWeight: 600, color: theme.textSecondary, marginBottom: 16, textTransform: "uppercase", letterSpacing: "0.04em" }}>
                    By Source
                  </h3>
                  <BarChart data={stats.sources} colorMap={{ manual: theme.accent, chat_import: "#64748b" }} />
                </div>
              </div>

              {/* Top Tags */}
              <div style={{ background: theme.surface, border: `1px solid ${theme.border}`, borderRadius: 8, padding: 20, marginTop: 16 }}>
                <h3 style={{ fontFamily: fontStack, fontSize: 13, fontWeight: 600, color: theme.textSecondary, marginBottom: 16, textTransform: "uppercase", letterSpacing: "0.04em" }}>
                  Top Tags
                </h3>
                <BarChart data={Object.fromEntries(Object.entries(stats.tags).sort((a, b) => b[1] - a[1]).slice(0, 10))} />
              </div>

              {/* Import info */}
              <div style={{
                background: theme.surface, border: `1px solid ${theme.border}`,
                borderRadius: 8, padding: 20, marginTop: 16,
              }}>
                <h3 style={{ fontFamily: fontStack, fontSize: 13, fontWeight: 600, color: theme.textSecondary, marginBottom: 12, textTransform: "uppercase", letterSpacing: "0.04em" }}>
                  MCP Server Info
                </h3>
                <div style={{ fontFamily: fontStack, fontSize: 13, color: theme.textSecondary, lineHeight: 2 }}>
                  <div><span style={{ color: theme.textMuted }}>Database:</span> <code style={{ color: theme.text }}>~/.remind-me/memory.db</code></div>
                  <div><span style={{ color: theme.textMuted }}>Transport:</span> <code style={{ color: theme.text }}>stdio</code></div>
                  <div><span style={{ color: theme.textMuted }}>Search engine:</span> <code style={{ color: theme.text }}>SQLite FTS5</code></div>
                  <div><span style={{ color: theme.textMuted }}>Sync:</span> <code style={{ color: theme.text }}>File-based (Syncthing, git, etc.)</code></div>
                </div>
              </div>
            </div>
          )}
        </main>
      </div>

      {/* Add Modal */}
      <Modal open={showAddModal} onClose={() => setShowAddModal(false)} title="Add Memory" width={520}>
        <MemoryForm onSubmit={handleAdd} onCancel={() => setShowAddModal(false)} />
      </Modal>

      {/* Edit Modal */}
      <Modal open={!!editMemory} onClose={() => setEditMemory(null)} title="Edit Memory" width={520}>
        {editMemory && <MemoryForm initial={editMemory} onSubmit={handleEdit} onCancel={() => setEditMemory(null)} />}
      </Modal>

      {/* Delete Confirmation */}
      <Modal open={!!deleteConfirm} onClose={() => setDeleteConfirm(null)} title="Delete Memory" width={400}>
        <p style={{ color: theme.textSecondary, fontFamily: sansStack, fontSize: 14, lineHeight: 1.6 }}>
          Are you sure you want to permanently delete memory <code style={{ fontFamily: fontStack, color: theme.text }}>{deleteConfirm}</code>? This cannot be undone.
        </p>
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 20 }}>
          <button onClick={() => setDeleteConfirm(null)} style={{
            padding: "8px 16px", borderRadius: 6, border: `1px solid ${theme.border}`,
            background: "transparent", color: theme.textSecondary, fontSize: 13,
            fontFamily: fontStack, cursor: "pointer",
          }}>Cancel</button>
          <button onClick={() => handleDelete(deleteConfirm)} style={{
            padding: "8px 20px", borderRadius: 6, border: "none",
            background: theme.danger, color: "#fff", fontSize: 13, fontWeight: 600,
            fontFamily: fontStack, cursor: "pointer",
          }}>Delete</button>
        </div>
      </Modal>
    </div>
  );
}