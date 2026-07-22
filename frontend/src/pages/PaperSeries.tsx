import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  api,
  Artifact,
  fmtDateTime,
  Job,
  paperAudienceLabel,
  PaperCoverage,
  PaperCoverageTopic,
  PaperDetail,
  PaperMemoryRevision,
  PaperMemoryState,
  PaperPartEvidence,
  PaperPlanOmission,
  PaperSeries as PaperSeriesModel,
  PaperSeriesPart,
  Project,
  typeLabel,
} from "../api";
import { useEventSource } from "../useEventSource";

interface SeriesDetailResponse {
  series: PaperSeriesModel;
  project?: Project;
  parts?: PaperSeriesPart[];
  coverage?: PaperCoverage;
  memory_revision?: PaperMemoryRevision | null;
  memory_revisions?: PaperMemoryRevision[];
  artifacts?: Artifact[];
  jobs?: Job[];
}

interface PartDraft {
  id: number;
  position: number;
  title: string;
  focus: string;
  evidence_ids: string[];
  evidence: PaperPartEvidence[];
  topics: string[];
  status?: string;
  stale?: boolean;
  locked?: boolean;
}

type TopicDestination = string; // "part:<id>", "omit", or ""

function errorMessage(value: unknown): string {
  return value instanceof Error ? value.message : "Unexpected error";
}

function parseMemory(revision?: PaperMemoryRevision | null): PaperMemoryState {
  if (!revision) return {};
  const raw = revision.state ?? revision.state_json;
  if (!raw) return {};
  if (typeof raw === "string") {
    try { return JSON.parse(raw) as PaperMemoryState; } catch { return {}; }
  }
  return raw;
}

function partEvidenceIds(part: PaperSeriesPart): string[] {
  if (part.evidence_ids) return [...part.evidence_ids];
  return (part.evidence ?? part.assignments ?? []).flatMap((assignment) =>
    typeof assignment === "string" ? [assignment] : assignment.evidence_id ? [assignment.evidence_id] : []);
}

function combinedParts(series: PaperSeriesModel, explicit?: PaperSeriesPart[]): PaperSeriesPart[] {
  const persisted = explicit ?? series.parts ?? [];
  const planned = series.plan?.parts ?? [];
  return persisted.map((part) => {
    const planPart = planned.find((candidate) => candidate.id === part.id || candidate.position === part.position);
    return planPart ? { ...part, ...planPart, id: part.id } : part;
  });
}

function partLocked(part: PaperSeriesPart): boolean {
  return part.structure_locked ?? part.locked ?? (
    ["complete", "finalized", "done"].includes(part.status ?? "")
    || ["complete", "done"].includes(part.script_status ?? "")
  );
}

function artifactFor(part: PaperSeriesPart, artifacts: Artifact[], kind: "guide" | "script" | "audio"): Artifact | null {
  const direct = kind === "guide" ? part.guide_artifact : kind === "script" ? part.script_artifact : part.audio_artifact;
  if (direct) return direct;
  const accepted = kind === "guide"
    ? ["paper_part_guide", "paper_study_guide"]
    : kind === "script" ? ["paper_part_script", "podcast_script"]
      : ["paper_part_audio", "podcast_audio"];
  return (part.artifacts ?? artifacts).find((artifact) => artifact.paper_part_id === part.id && accepted.includes(artifact.type)) ?? null;
}

function normalizeDetail(value: SeriesDetailResponse | PaperSeriesModel): SeriesDetailResponse {
  return "series" in value ? value : {
    series: value,
    parts: value.parts,
    coverage: value.coverage ?? value.plan?.coverage,
    memory_revision: value.memory_revision,
    memory_revisions: value.memory_revisions,
    artifacts: value.artifacts,
    jobs: value.jobs,
  };
}

function memoryEntries(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => {
    if (typeof item === "string") return item;
    if (item && typeof item === "object") {
      const record = item as Record<string, unknown>;
      return [record.term, record.pronunciation && `/${record.pronunciation}/`, record.meaning]
        .filter(Boolean).join(" — ");
    }
    return String(item);
  });
}

function normalizedImportance(value?: string): "critical" | "major" | "supporting" {
  return value === "critical" || value === "major" ? value : "supporting";
}

export default function PaperSeries() {
  const { id } = useParams();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const [detail, setDetail] = useState<SeriesDetailResponse | null>(null);
  const [drafts, setDrafts] = useState<PartDraft[]>([]);
  const [topicDestinations, setTopicDestinations] = useState<Record<string, TopicDestination>>({});
  const [evidenceDestinations, setEvidenceDestinations] = useState<Record<string, TopicDestination>>({});
  const [omissionReasons, setOmissionReasons] = useState<Record<string, string>>({});
  const [evidenceOmissionReasons, setEvidenceOmissionReasons] = useState<Record<string, string>>({});
  const [guidance, setGuidance] = useState("");
  const [targetMinutes, setTargetMinutes] = useState(50);
  const [dirty, setDirty] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [action, setAction] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [showPlan, setShowPlan] = useState(true);

  const applyDetail = useCallback((next: SeriesDetailResponse) => {
    const series = next.series;
    const parts = combinedParts(series, next.parts);
    const nextDrafts = [...parts]
      .sort((a, b) => a.position - b.position)
      .map((part) => ({
        id: part.id,
        position: part.position,
        title: part.title,
        focus: part.focus ?? "",
        evidence_ids: [],
        evidence: [...(part.evidence ?? [])],
        topics: [...(part.topics ?? [])],
        status: part.status,
        stale: part.stale,
        locked: partLocked(part),
      }));
    setDrafts(nextDrafts);
    setGuidance(series.user_guidance ?? "");
    setTargetMinutes(series.target_minutes ?? 50);

    const topics = next.coverage?.topics ?? series.coverage?.topics ?? series.plan?.topics ?? [];
    const destinations: Record<string, TopicDestination> = {};
    for (const topic of topics) {
      const matchingPart = topic.assigned_part_id
        ? nextDrafts.find((part) => part.id === topic.assigned_part_id)
        : topic.assigned_part
          ? nextDrafts.find((part) => part.position === topic.assigned_part)
          : nextDrafts.find((part) => part.topics.includes(topic.id)
            || part.evidence_ids.some((evidenceId) => topic.evidence_ids?.includes(evidenceId)));
      destinations[topic.id] = topic.omitted ? "omit" : matchingPart ? `part:${matchingPart.id}` : "";
    }
    setTopicDestinations(destinations);
    const omissions = next.series.omissions ?? next.series.plan?.omissions ?? [];
    setOmissionReasons(Object.fromEntries(
      omissions.filter((omission) => omission.topic_id).map((omission) => [omission.topic_id as string, omission.reason]),
    ));
    const evidenceHomes: Record<string, TopicDestination> = {};
    for (const part of nextDrafts) {
      for (const evidence of part.evidence) {
        if (evidence.evidence_id && evidence.role !== "bridge") {
          evidenceHomes[evidence.evidence_id] = `part:${part.id}`;
        }
      }
    }
    const evidenceReasons: Record<string, string> = {};
    for (const omission of omissions) {
      if (omission.evidence_id) {
        evidenceHomes[omission.evidence_id] = "omit";
        evidenceReasons[omission.evidence_id] = omission.reason;
      }
    }
    setEvidenceDestinations(evidenceHomes);
    setEvidenceOmissionReasons(evidenceReasons);
    setDirty(false);
  }, []);

  const load = useCallback(async (preserveDraft = false) => {
    if (!id) return;
    setLoading(true);
    try {
      const next = normalizeDetail(await api<SeriesDetailResponse | PaperSeriesModel>(`/paper-series/${id}`));
      if (!next.project) {
        try {
          const paper = await api<PaperDetail>(`/papers/${next.series.project_id}`);
          next.project = paper.project;
          next.coverage ??= paper.coverage;
        } catch {
          // The track remains usable if project-level context is temporarily unavailable.
        }
      }
      setDetail(next);
      if (!preserveDraft || !dirty) applyDetail(next);
      setError("");
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setLoading(false);
    }
  }, [applyDetail, dirty, id]);

  useEffect(() => { void load(); }, [id]); // load intentionally resets draft when the route changes
  useEventSource(`/api/jobs/stream?paper_series_id=${id ?? ""}`, "jobs", () => void load(true));

  const series = detail?.series;
  const project = detail?.project;
  const storedParts = series ? combinedParts(series, detail?.parts) : [];
  const artifacts = detail?.artifacts ?? series?.artifacts ?? [];
  const coverage = detail?.coverage ?? series?.coverage ?? series?.plan?.coverage;
  const topics = coverage?.topics ?? series?.plan?.topics ?? series?.plan?.critical_topics ?? [];
  const latestMemory = detail?.memory_revision ?? series?.memory_revision
    ?? detail?.memory_revisions?.at(-1) ?? series?.memory_revisions?.at(-1);
  const memory = parseMemory(latestMemory);
  const selectedPartId = Number(searchParams.get("part"));
  const selectedDraft = drafts.find((part) => part.id === selectedPartId) ?? drafts[0];
  const selectedStoredPart = storedParts.find((part) => part.id === selectedDraft?.id);
  const planVersion = series?.plan_version ?? series?.plan?.version ?? 1;
  const isDraftPlan = ["draft", "planning", "proposed"].includes(series?.status ?? "draft");
  const criticalTopics = topics.filter((topic) => topic.importance === "critical");
  const evidenceCatalog = useMemo(() => {
    const values = new Map<string, PaperPartEvidence>();
    for (const part of drafts) {
      for (const evidence of part.evidence) {
        if (evidence.evidence_id && evidence.role !== "bridge" && !values.has(evidence.evidence_id)) {
          values.set(evidence.evidence_id, evidence);
        }
      }
    }
    return [...values.values()].sort((left, right) =>
      (left.page ?? 0) - (right.page ?? 0) || (left.evidence_id ?? "").localeCompare(right.evidence_id ?? ""));
  }, [drafts]);
  const highPriorityEvidence = evidenceCatalog.filter((evidence) =>
    evidence.importance === "critical" || evidence.importance === "major");
  const unassignedCritical = criticalTopics.filter((topic) => !topicDestinations[topic.id]);
  const criticalOmissionsWithoutReason = criticalTopics.filter((topic) =>
    topicDestinations[topic.id] === "omit" && !omissionReasons[topic.id]?.trim());
  const incompleteCriticalEvidence = evidenceCatalog.filter((evidence) =>
    evidence.importance === "critical" && (
      !evidenceDestinations[evidence.evidence_id ?? ""]
      || evidenceDestinations[evidence.evidence_id ?? ""] === "omit"
        && !evidenceOmissionReasons[evidence.evidence_id ?? ""]?.trim()
    ));
  const canApprove = drafts.length >= 1 && drafts.length <= 5
    && drafts.every((part) => part.title.trim() && part.focus.trim())
    && targetMinutes >= 40 && targetMinutes <= 60
    && unassignedCritical.length === 0 && criticalOmissionsWithoutReason.length === 0
    && incompleteCriticalEvidence.length === 0 && !dirty;

  function updateDraft(partId: number, patch: Partial<PartDraft>) {
    setDrafts((current) => current.map((part) => part.id === partId ? { ...part, ...patch } : part));
    setDirty(true);
  }

  function movePart(partId: number, direction: -1 | 1) {
    setDrafts((current) => {
      const index = current.findIndex((part) => part.id === partId);
      const target = index + direction;
      if (index < 0 || target < 0 || target >= current.length || current[index].locked || current[target].locked) return current;
      const copy = [...current];
      [copy[index], copy[target]] = [copy[target], copy[index]];
      return copy.map((part, position) => ({ ...part, position: position + 1 }));
    });
    setDirty(true);
  }

  function addPart() {
    if (drafts.length >= 5) return;
    const temporaryId = -Date.now();
    setDrafts((current) => [...current, {
      id: temporaryId,
      position: current.length + 1,
      title: `Part ${current.length + 1}`,
      focus: "",
      evidence_ids: [],
      evidence: [],
      topics: [],
    }]);
    setDirty(true);
  }

  function removePart(part: PartDraft) {
    if (part.locked || drafts.length <= 1) return;
    setDrafts((current) => current.filter((item) => item.id !== part.id)
      .map((item, index) => ({ ...item, position: index + 1 })));
    setTopicDestinations((current) => Object.fromEntries(
      Object.entries(current).map(([topicId, destination]) => [
        topicId,
        destination === `part:${part.id}` ? "" : destination,
      ]),
    ));
    setEvidenceDestinations((current) => Object.fromEntries(
      Object.entries(current).map(([evidenceId, destination]) => [
        evidenceId,
        destination === `part:${part.id}` ? "" : destination,
      ]),
    ));
    setDirty(true);
  }

  function assignTopic(topic: PaperCoverageTopic, destination: TopicDestination) {
    setTopicDestinations((current) => ({ ...current, [topic.id]: destination }));
    if (destination.startsWith("part:")) {
      setEvidenceDestinations((current) => ({
        ...current,
        ...Object.fromEntries((topic.evidence_ids ?? []).map((evidenceId) => [evidenceId, destination])),
      }));
    }
    setDirty(true);
  }

  function assignEvidence(evidenceId: string, destination: TopicDestination) {
    setEvidenceDestinations((current) => ({ ...current, [evidenceId]: destination }));
    setDirty(true);
  }

  async function savePlan(event?: FormEvent) {
    event?.preventDefault();
    if (!series) return;
    if (targetMinutes < 40 || targetMinutes > 60) {
      setError("The per-part planning target must be between 40 and 60 minutes.");
      return;
    }
    const incompleteOmission = topics.find((topic) =>
      topicDestinations[topic.id] === "omit" && !omissionReasons[topic.id]?.trim());
    if (incompleteOmission) {
      setError(`Record a reason for omitting “${incompleteOmission.title}”.`);
      return;
    }
    const incompleteEvidenceOmission = evidenceCatalog.find((evidence) => {
      const evidenceId = evidence.evidence_id ?? "";
      return evidenceDestinations[evidenceId] === "omit" && !evidenceOmissionReasons[evidenceId]?.trim();
    });
    if (incompleteEvidenceOmission) {
      setError(`Record a reason for omitting evidence ${incompleteEvidenceOmission.evidence_id}.`);
      return;
    }

    setSaving(true);
    setError("");
    setNotice("");
    const topicOmissions: PaperPlanOmission[] = topics
      .filter((topic) => topicDestinations[topic.id] === "omit")
      .map((topic) => ({
        topic_id: topic.id,
        reason: omissionReasons[topic.id].trim(),
        importance: normalizedImportance(topic.importance),
        demoted_from: topic.importance === "critical" ? "critical"
          : topic.importance === "major" ? "major" : null,
      }));
    const evidenceOmissions: PaperPlanOmission[] = evidenceCatalog
      .filter((evidence) => evidence.evidence_id && evidenceDestinations[evidence.evidence_id] === "omit")
      .map((evidence) => ({
        evidence_id: evidence.evidence_id,
        reason: evidenceOmissionReasons[evidence.evidence_id ?? ""].trim(),
        importance: normalizedImportance(evidence.importance),
        demoted_from: evidence.importance === "critical" ? "critical"
          : evidence.importance === "major" ? "major" : null,
      }));
    const omissions = [...topicOmissions, ...evidenceOmissions];
    const parts = drafts.map((part) => {
      const assignedTopics = topics.filter((topic) => topicDestinations[topic.id] === `part:${part.id}`);
      const evidenceById = new Map<string, PaperPartEvidence>();
      for (const evidence of part.evidence.filter((item) => item.role === "bridge")) {
        if (evidence.evidence_id) evidenceById.set(evidence.evidence_id, evidence);
      }
      for (const evidence of evidenceCatalog) {
        const evidenceId = evidence.evidence_id;
        if (!evidenceId || evidenceDestinations[evidenceId] !== `part:${part.id}`) continue;
        evidenceById.set(evidenceId, { ...evidence, role: "primary" });
      }
      for (const evidenceId of part.evidence_ids) {
        if (evidenceById.has(evidenceId)) continue;
        evidenceById.set(evidenceId, {
          evidence_id: evidenceId,
          role: "primary",
          importance: "supporting",
        });
      }
      return {
        ...(part.id > 0 ? { id: part.id } : {}),
        position: part.position,
        title: part.title.trim(),
        focus: part.focus.trim(),
        target_minutes: targetMinutes,
        topics: assignedTopics.map((topic) => topic.id),
        evidence_ids: [...evidenceById.keys()],
        evidence: [...evidenceById.values()].map((evidence) => ({
          evidence_id: evidence.evidence_id,
          role: evidence.role ?? "primary",
          importance: normalizedImportance(evidence.importance),
          reason: evidence.reason ?? "",
        })),
      };
    });
    try {
      await api(`/paper-series/${series.id}/plan`, {
        method: "PUT",
        body: JSON.stringify({
          expected_version: planVersion,
          target_minutes: targetMinutes,
          parts,
          omissions,
          critical_topics: series.plan?.critical_topics ?? criticalTopics,
          user_guidance: guidance,
        }),
      });
      setNotice("Plan saved. Any affected ungenerated outputs are marked stale.");
      await load();
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setSaving(false);
    }
  }

  async function approvePlan() {
    if (!series || !canApprove) return;
    setAction("approve");
    setError("");
    try {
      await api(`/paper-series/${series.id}/approve`, {
        method: "POST",
        body: JSON.stringify({ expected_version: planVersion }),
      });
      setNotice("Audience plan approved. It is ready for production.");
      setShowPlan(false);
      await load();
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setAction("");
    }
  }

  async function runSeries() {
    if (!series) return;
    setAction("run");
    setError("");
    try {
      await api(`/paper-series/${series.id}/run`, { method: "POST" });
      setNotice("Series production queued. Study guides can run in parallel; scripts will proceed in order.");
      await load(true);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setAction("");
    }
  }

  async function runPartStep(part: PaperSeriesPart, step: "guide" | "script" | "audio") {
    if (!series) return;
    setAction(`${part.id}:${step}`);
    setError("");
    try {
      await api(`/paper-series/${series.id}/parts/${part.id}/run/${step}`, { method: "POST" });
      setNotice(`${step === "guide" ? "Study guide" : step === "script" ? "Script" : "Audio"} queued for Part ${part.position}.`);
      await load(true);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setAction("");
    }
  }

  async function rebuildFollowing(part: PaperSeriesPart) {
    if (!series || !confirm(
      `Rebuild Part ${part.position}'s script and following parts?\n\n` +
      "Existing later scripts and audio will be preserved but marked stale until their replacements complete.",
    )) return;
    setAction(`${part.id}:rebuild`);
    setError("");
    try {
      await api(`/paper-series/${series.id}/parts/${part.id}/rebuild-following`, { method: "POST" });
      setNotice(`Rebuild queued from Part ${part.position}. Later scripts and audio are now visibly stale.`);
      await load(true);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setAction("");
    }
  }

  async function deleteSeries() {
    if (!series || !confirm(
      `Delete this ${paperAudienceLabel(series.audience)} track?\n\nThe source paper and other tracks remain.`,
    )) return;
    setAction("delete");
    setError("");
    try {
      await api(`/paper-series/${series.id}`, { method: "DELETE" });
      navigate(project ? `/projects/${project.id}` : "/projects");
    } catch (caught) {
      setError(errorMessage(caught));
      setAction("");
    }
  }

  if (!series) {
    return (
      <section className="loading-state">
        {loading && <p role="status">Loading audience series…</p>}
        {error && <><p className="error" role="alert">{error}</p><Link to="/projects">Back to projects</Link></>}
      </section>
    );
  }

  const audienceLabel = paperAudienceLabel(series.audience);
  const trackArtifacts = artifacts.filter((artifact) => artifact.paper_series_id === series.id && !artifact.paper_part_id);
  const runningJobs = (detail?.jobs ?? []).filter((job) => ["queued", "running"].includes(job.status));

  return (
    <div className="paper-series-page">
      <nav className="artifact-breadcrumbs" aria-label="Breadcrumb">
        <Link to="/projects">Projects</Link><span aria-hidden="true">›</span>
        {project ? <Link to={`/projects/${project.id}`}>{project.title}</Link> : <span>Paper</span>}
        <span aria-hidden="true">›</span><span>{audienceLabel} series</span>
      </nav>

      <header className="paper-series-head">
        <div>
          <span className={`source-badge audience ${series.audience}`}>{audienceLabel}</span>
          <h2>{series.title || `${audienceLabel} audience series`}</h2>
          <p className="lead">A prerequisite-aware teaching arc grounded in the paper’s shared evidence map.</p>
        </div>
        <div className="paper-series-actions">
          <span className={`jobstatus ${series.status === "complete" ? "done" : isDraftPlan ? "new" : "running"}`}>{series.status}</span>
          {!isDraftPlan && <button type="button" className="primary" onClick={() => void runSeries()} disabled={action !== "" || runningJobs.length > 0}>
            {action === "run" ? "Queuing…" : runningJobs.length ? "Production running" : "Run approved track"}
          </button>}
          <button type="button" className="linkish danger" onClick={() => void deleteSeries()} disabled={action !== ""}>
            {action === "delete" ? "Deleting…" : "Delete track"}
          </button>
        </div>
      </header>

      {error && <p className="error" role="alert">{error}</p>}
      {notice && <p className="notice" role="status">{notice}</p>}

      <section className="paper-plan-summary card" aria-labelledby="paper-plan-summary-title">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Approval gate</p>
            <h3 id="paper-plan-summary-title">Audience plan · version {planVersion}</h3>
          </div>
          <button type="button" onClick={() => setShowPlan((current) => !current)} aria-expanded={showPlan}>
            {showPlan ? "Hide editor" : "Review and edit plan"}
          </button>
        </div>
        <div className="paper-coverage-stats">
          <span><b>{drafts.length}</b> / 5 parts</span>
          <span><b>{targetMinutes}</b> minute target per part</span>
          <span><b>{coverage?.critical_assigned ?? criticalTopics.length - unassignedCritical.length}</b> / {coverage?.critical_total ?? criticalTopics.length} critical topics assigned</span>
          <span><b>{coverage?.major_assigned ?? "—"}</b> / {coverage?.major_total ?? "—"} major topics assigned</span>
          <span><b>{coverage?.pages_admitted ?? "—"}</b> / {coverage?.pages_total ?? "—"} pages admitted</span>
        </div>
        {unassignedCritical.length > 0 && (
          <p className="error" role="alert">{unassignedCritical.length} critical topic{unassignedCritical.length === 1 ? " is" : "s are"} still unassigned.</p>
        )}
        {incompleteCriticalEvidence.length > 0 && (
          <p className="error" role="alert">
            {incompleteCriticalEvidence.length} critical evidence block{incompleteCriticalEvidence.length === 1 ? " needs" : "s need"} a primary part or a recorded demotion reason.
          </p>
        )}
        {dirty && <p className="notice">You have unsaved plan changes. Approval uses the last saved version.</p>}
      </section>

      {showPlan && (
        <form className="paper-plan-editor" onSubmit={savePlan}>
          <section aria-labelledby="paper-parts-editor-title">
            <div className="section-heading">
              <div>
                <p className="eyebrow">Teaching arc</p>
                <h3 id="paper-parts-editor-title">Parts</h3>
              </div>
              <div className="paper-plan-target">
                <label>Minutes per part
                  <input type="number" min={40} max={60} value={targetMinutes}
                    onChange={(event) => { setTargetMinutes(Number(event.target.value)); setDirty(true); }} />
                </label>
                <button type="button" onClick={addPart} disabled={drafts.length >= 5 || !isDraftPlan}>Add part</button>
              </div>
            </div>
            <div className="paper-part-editor-list">
              {drafts.map((part, index) => (
                <article className={`card paper-part-editor ${part.locked ? "locked" : ""}`} key={part.id}>
                  <div className="paper-part-editor-head">
                    <b>Part {index + 1}</b>
                    {part.locked && <span className="jobstatus done">Structure locked</span>}
                    {part.stale && <span className="jobstatus partial">Affected outputs stale</span>}
                    <span className="paper-order-actions">
                      <button type="button" aria-label={`Move Part ${index + 1} earlier`}
                        onClick={() => movePart(part.id, -1)} disabled={index === 0 || part.locked}>↑</button>
                      <button type="button" aria-label={`Move Part ${index + 1} later`}
                        onClick={() => movePart(part.id, 1)} disabled={index === drafts.length - 1 || part.locked}>↓</button>
                      <button type="button" className="linkish danger" onClick={() => removePart(part)}
                        disabled={drafts.length <= 1 || part.locked || !isDraftPlan}>Remove</button>
                    </span>
                  </div>
                  <label>
                    Title
                    <input value={part.title} onChange={(event) => updateDraft(part.id, { title: event.target.value })}
                      disabled={part.locked} required />
                  </label>
                  <label>
                    Focus and learning outcome
                    <textarea rows={3} value={part.focus}
                      onChange={(event) => updateDraft(part.id, { focus: event.target.value })}
                      disabled={part.locked} required />
                  </label>
                  <label>
                    Additional evidence IDs <small>(comma-separated; {part.evidence.filter((item) => item.role !== "bridge").length} mapped primary and {part.evidence.filter((item) => item.role === "bridge").length} bridge assignments are tracked below)</small>
                    <textarea rows={2} value={part.evidence_ids.join(", ")}
                      onChange={(event) => updateDraft(part.id, {
                        evidence_ids: event.target.value.split(/[\s,]+/).map((item) => item.trim()).filter(Boolean),
                      })} disabled={part.locked} />
                  </label>
                </article>
              ))}
            </div>
          </section>

          {topics.length > 0 && (
            <section className="paper-topic-assignments" aria-labelledby="paper-topic-assignments-title">
              <div className="section-heading">
                <div>
                  <p className="eyebrow">Coverage ledger</p>
                  <h3 id="paper-topic-assignments-title">Major topic assignments</h3>
                </div>
                <span className="meta">Each topic needs one primary home. Bridges and callbacks remain bounded.</span>
              </div>
              <div className="table-scroll" tabIndex={0}>
                <table className="list paper-topic-table">
                  <thead><tr><th>Topic</th><th>Importance</th><th>Primary home</th><th>Omission reason</th></tr></thead>
                  <tbody>
                    {topics.map((topic) => {
                      const destination = topicDestinations[topic.id] ?? "";
                      return (
                        <tr key={topic.id} className={!destination && topic.importance === "critical" ? "coverage-missing" : ""}>
                          <td><b>{topic.title}</b>{topic.kind && <small>{topic.kind}</small>}</td>
                          <td><span className={`importance ${topic.importance ?? "supporting"}`}>{topic.importance ?? "supporting"}</span></td>
                          <td>
                            <label className="sr-only" htmlFor={`topic-${topic.id}`}>Primary home for {topic.title}</label>
                            <select id={`topic-${topic.id}`} value={destination}
                              onChange={(event) => assignTopic(topic, event.target.value)}>
                              <option value="">Unassigned</option>
                              {drafts.map((part) => <option key={part.id} value={`part:${part.id}`}>Part {part.position}: {part.title}</option>)}
                              <option value="omit">Omit with reason</option>
                            </select>
                          </td>
                          <td>
                            {destination === "omit" ? (
                              <input value={omissionReasons[topic.id] ?? ""}
                                onChange={(event) => { setOmissionReasons((current) => ({ ...current, [topic.id]: event.target.value })); setDirty(true); }}
                                placeholder={topic.importance === "critical" ? "Required; explain critical demotion" : "Required reason"} />
                            ) : <span className="meta">—</span>}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </section>
          )}

          {highPriorityEvidence.length > 0 && (
            <section className="paper-topic-assignments" aria-labelledby="paper-evidence-assignments-title">
              <div className="section-heading">
                <div>
                  <p className="eyebrow">Evidence ledger</p>
                  <h3 id="paper-evidence-assignments-title">Critical and major evidence</h3>
                </div>
                <span className="meta">Every critical block needs one primary part or an explicit demotion reason.</span>
              </div>
              <div className="table-scroll" tabIndex={0}>
                <table className="list paper-topic-table paper-evidence-table">
                  <thead><tr><th>Evidence</th><th>Importance</th><th>Primary home</th><th>Omission reason</th></tr></thead>
                  <tbody>
                    {highPriorityEvidence.map((evidence) => {
                      const evidenceId = evidence.evidence_id ?? "";
                      const destination = evidenceDestinations[evidenceId] ?? "";
                      return (
                        <tr key={evidenceId} className={!destination && evidence.importance === "critical" ? "coverage-missing" : ""}>
                          <td>
                            <b><code>{evidenceId}</code></b>
                            <small>
                              {evidence.page ? <a href={`/api/papers/${series.project_id}/source#page=${evidence.page}`} target="_blank" rel="noreferrer">Page {evidence.page}</a> : "Page not recorded"}
                              {evidence.section ? ` · ${evidence.section}` : ""}
                            </small>
                          </td>
                          <td><span className={`importance ${evidence.importance ?? "supporting"}`}>{evidence.importance ?? "supporting"}</span></td>
                          <td>
                            <label className="sr-only" htmlFor={`evidence-${evidenceId}`}>Primary home for {evidenceId}</label>
                            <select id={`evidence-${evidenceId}`} value={destination}
                              onChange={(event) => assignEvidence(evidenceId, event.target.value)}>
                              <option value="">Unassigned</option>
                              {drafts.map((part) => <option key={part.id} value={`part:${part.id}`}>Part {part.position}: {part.title}</option>)}
                              <option value="omit">Demote/omit with reason</option>
                            </select>
                          </td>
                          <td>
                            {destination === "omit" ? (
                              <input value={evidenceOmissionReasons[evidenceId] ?? ""}
                                onChange={(event) => {
                                  setEvidenceOmissionReasons((current) => ({ ...current, [evidenceId]: event.target.value }));
                                  setDirty(true);
                                }} placeholder={evidence.importance === "critical" ? "Required critical demotion reason" : "Required reason"} />
                            ) : <span className="meta">—</span>}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </section>
          )}

          <section className="card paper-guidance" aria-labelledby="paper-guidance-title">
            <h3 id="paper-guidance-title">Your guidance for the hosts</h3>
            <p className="meta">This is stored separately from generated facts in the series bible.</p>
            <textarea rows={4} value={guidance} onChange={(event) => { setGuidance(event.target.value); setDirty(true); }}
              placeholder="Tone, emphasis, analogies to avoid, pronunciation preferences, or audience context" />
          </section>

          <div className="paper-plan-actions">
            <button type="submit" disabled={!dirty || saving}>{saving ? "Saving version…" : "Save plan"}</button>
            {isDraftPlan && (
              <button type="button" className="primary" onClick={() => void approvePlan()}
                disabled={!canApprove || action !== ""} title={!canApprove ? "Save a complete, valid critical-topic plan first." : undefined}>
                {action === "approve" ? "Approving…" : "Approve this audience track"}
              </button>
            )}
          </div>
        </form>
      )}

      {trackArtifacts.length > 0 && (
        <section className="paper-track-suite" aria-labelledby="paper-track-suite-title">
          <div className="section-heading"><div><p className="eyebrow">Audience-specific suite</p><h3 id="paper-track-suite-title">Whole-track guides</h3></div></div>
          <div className="repository-guide-grid">
            {trackArtifacts.map((artifact) => (
              <Link className="card related-artifact" to={`/artifacts/${artifact.id}`} key={artifact.id}>
                <span className="kindbadge">{typeLabel(artifact.type)}</span><strong>{artifact.title}</strong>
              </Link>
            ))}
          </div>
        </section>
      )}

      <section className="paper-production" aria-labelledby="paper-production-title">
        <div className="section-heading">
          <div><p className="eyebrow">Sequential continuity</p><h3 id="paper-production-title">Parts and production</h3></div>
          <span className="meta">Guides may run in parallel · scripts follow the previous memory revision</span>
        </div>
        <nav className="paper-part-tabs" aria-label="Series parts">
          {drafts.map((part) => (
            <button type="button" key={part.id} className={selectedDraft?.id === part.id ? "on" : ""}
              onClick={() => setSearchParams({ part: String(part.id) })}>
              <span>Part {part.position}</span>
              <small>{part.stale ? "Stale" : part.status ?? "Planned"}</small>
            </button>
          ))}
        </nav>

        {selectedDraft && selectedStoredPart ? (
          <article className="paper-part-detail card">
            <div className="paper-part-title-row">
              <div><span className="eyebrow">Part {selectedDraft.position} · {targetMinutes} minute target</span><h3>{selectedDraft.title}</h3></div>
              <span className={`jobstatus ${selectedDraft.stale ? "partial" : selectedDraft.status === "complete" ? "done" : "new"}`}>
                {selectedDraft.stale ? "Following output stale" : selectedDraft.status ?? "planned"}
              </span>
            </div>
            <p>{selectedDraft.focus}</p>
            <div className="paper-part-step-grid">
              {(["guide", "script", "audio"] as const).map((step) => {
                const artifact = artifactFor(selectedStoredPart, artifacts, step);
                const stepStatus = selectedStoredPart[`${step}_status` as "guide_status" | "script_status" | "audio_status"] ?? "pending";
                const job = (selectedStoredPart.jobs ?? detail?.jobs ?? []).find((item) =>
                  item.paper_part_id === selectedStoredPart.id && item.task.toLocaleLowerCase().includes(step));
                const active = ["queued", "running", "generating"].includes(stepStatus)
                  || Boolean(job && ["queued", "running"].includes(job.status));
                return (
                  <section className={`paper-part-step ${artifact ? "complete" : ""}`} key={step}>
                    <h4>{step === "guide" ? "Study guide + show notes" : step === "script" ? "Two-host script" : "Podcast audio"}</h4>
                    <p className="meta">{active ? job?.progress || stepStatus : artifact ? `Updated ${fmtDateTime(artifact.updated)}` : stepStatus === "stale" ? "Stale — rebuild required" : "Not generated"}</p>
                    {artifact ? <Link to={`/artifacts/${artifact.id}`}>Open {step}</Link> : (
                      <button type="button" onClick={() => void runPartStep(selectedStoredPart, step)}
                        disabled={Boolean(active) || action !== "" || isDraftPlan}>
                        {action === `${selectedStoredPart.id}:${step}` ? "Queuing…" : `Generate ${step}`}
                      </button>
                    )}
                    {step === "audio" && artifact?.media_path && <audio controls src={`/api/media/${artifact.id}`} />}
                  </section>
                );
              })}
            </div>
            {artifactFor(selectedStoredPart, artifacts, "script") && (
              <div className="paper-rebuild-action">
                <div>
                  <b>Changed this script?</b>
                  <p className="meta">A new memory revision will make later scripts and audio stale while preserving them.</p>
                </div>
                <button type="button" onClick={() => void rebuildFollowing(selectedStoredPart)} disabled={action !== ""}>
                  {action === `${selectedStoredPart.id}:rebuild` ? "Queuing rebuild…" : "Rebuild this and following"}
                </button>
              </div>
            )}
          </article>
        ) : drafts.length > 0 ? (
          <p className="meta">Save the plan to create part records and production controls.</p>
        ) : (
          <p className="meta">No parts have been planned.</p>
        )}
      </section>

      <section className="paper-series-bible" aria-labelledby="paper-series-bible-title">
        <div className="section-heading">
          <div><p className="eyebrow">Immutable continuity ledger</p><h3 id="paper-series-bible-title">Series bible</h3></div>
          {latestMemory && <span className="meta">Revision {latestMemory.revision}{latestMemory.created ? ` · ${fmtDateTime(latestMemory.created)}` : ""}</span>}
        </div>
        {latestMemory ? (
          <div className="series-bible-grid">
            {[
              ["Terminology and pronunciations", memory.terminology],
              ["Introduced topics", memory.introduced_topics],
              ["Completed topics", memory.completed_topics],
              ["Deferred topics", memory.deferred_topics],
              ["Claims already covered", memory.covered_claims],
              ["Examples used", memory.examples],
              ["Stories and analogies", memory.stories_and_analogies],
              ["Open questions", memory.open_questions],
              ["Promised callbacks", memory.promised_callbacks],
              ["Handoff notes", memory.handoff_notes],
            ].map(([label, values]) => {
              const entries = memoryEntries(values);
              return (
                <article className="card" key={label as string}>
                  <h4>{label as string}</h4>
                  {entries.length ? <ul>{entries.map((entry, index) => <li key={`${entry}:${index}`}>{entry}</li>)}</ul> : <p className="meta">None recorded yet.</p>}
                </article>
              );
            })}
          </div>
        ) : (
          <p className="meta">The first immutable memory revision appears after Part 1’s script is finalized.</p>
        )}
        {series.user_guidance && (
          <aside className="card paper-user-guidance"><h4>Your separate guidance</h4><p>{series.user_guidance}</p></aside>
        )}
      </section>
    </div>
  );
}
