import {
  Job,
  JobDiagnosticAttempt,
  JobDiagnostics,
} from "../api";

type UnknownRecord = Record<string, unknown>;

function isRecord(value: unknown): value is UnknownRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function textValue(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function numberValue(value: unknown): number | undefined {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value !== "string" || !value.trim()) return undefined;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function booleanValue(value: unknown): boolean | undefined {
  return typeof value === "boolean" ? value : undefined;
}

function stringArray(value: unknown): string[] | undefined {
  if (!Array.isArray(value)) return undefined;
  const values = value.map(textValue).filter((item): item is string => Boolean(item));
  return values.length ? values : undefined;
}

function numberRecord(value: unknown): Record<string, number> | undefined {
  if (!isRecord(value)) return undefined;
  const values = Object.entries(value).reduce<Record<string, number>>((result, [key, item]) => {
    const parsed = numberValue(item);
    if (parsed !== undefined) result[key] = parsed;
    return result;
  }, {});
  return Object.keys(values).length ? values : undefined;
}

function parseResidentTransition(value: unknown) {
  if (!isRecord(value)) return undefined;
  const transition = {
    required: booleanValue(value.required),
    resident_models: stringArray(value.resident_models),
    replaced_models: stringArray(value.replaced_models),
    reclaimable_ram_bytes: numberValue(value.reclaimable_ram_bytes),
    reclaimable_vram_bytes: numberValue(value.reclaimable_vram_bytes),
  };
  return Object.values(transition).some((item) => item !== undefined)
    ? transition
    : undefined;
}

function parseAttempt(value: unknown): JobDiagnosticAttempt | null {
  if (!isRecord(value)) return null;
  const attempt: JobDiagnosticAttempt = {
    outcome: textValue(value.outcome),
    level: numberValue(value.level),
    batch: numberValue(value.batch),
    depth: numberValue(value.depth),
    detail: textValue(value.detail),
  };
  return Object.values(attempt).some((item) => item !== undefined) ? attempt : null;
}

function parseDiagnosticsObject(value: UnknownRecord): JobDiagnostics | null {
  const effectiveModel = isRecord(value.effective_model)
    ? {
        provider: textValue(value.effective_model.provider),
        model: textValue(value.effective_model.model),
        digest: textValue(value.effective_model.digest),
      }
    : undefined;
  const contextRecord = isRecord(value.context) ? value.context : undefined;
  const safetyRecord = contextRecord && isRecord(contextRecord.safety_assessment)
    ? contextRecord.safety_assessment
    : undefined;
  const nestedTransition = safetyRecord
    ? parseResidentTransition(safetyRecord.resident_transition)
    : undefined;
  const residentTransition = contextRecord
    ? parseResidentTransition(contextRecord.resident_transition) || nestedTransition
    : undefined;
  const safetyAssessment = safetyRecord
    ? {
        tier: textValue(safetyRecord.tier),
        message: textValue(safetyRecord.message),
        requested_context_tokens: numberValue(safetyRecord.requested_context_tokens),
        estimated_total_bytes: numberValue(safetyRecord.estimated_total_bytes),
        acknowledged: booleanValue(safetyRecord.acknowledged),
        resident_transition: nestedTransition,
      }
    : undefined;
  const context = contextRecord
    ? {
        requested: numberValue(contextRecord.requested),
        effective: numberValue(contextRecord.effective),
        native: numberValue(contextRecord.native),
        timeout_seconds: numberValue(contextRecord.timeout_seconds),
        max_output_tokens: numberValue(contextRecord.max_output_tokens),
        safety_assessment: safetyAssessment
          && Object.values(safetyAssessment).some((item) => item !== undefined)
          ? safetyAssessment
          : undefined,
        resident_transition: residentTransition,
      }
    : undefined;
  const reduction = isRecord(value.reduction)
    ? {
        purpose: textValue(value.reduction.purpose),
        level: numberValue(value.reduction.level),
        batch: numberValue(value.reduction.batch),
        batch_count: numberValue(value.reduction.batch_count),
        items: numberValue(value.reduction.items),
        input_chars: numberValue(value.reduction.input_chars),
        subdivision_depth: numberValue(value.reduction.subdivision_depth),
        complete: booleanValue(value.reduction.complete),
      }
    : undefined;
  const cache = isRecord(value.cache)
    ? {
        leaf_maps_reused: numberValue(value.cache.leaf_maps_reused),
        leaf_maps_new: numberValue(value.cache.leaf_maps_new),
        legacy_leaf_maps_reused: numberValue(value.cache.legacy_leaf_maps_reused),
        reductions_reused: numberValue(value.cache.reductions_reused),
        reductions_new: numberValue(value.cache.reductions_new),
      }
    : undefined;
  const attempts = Array.isArray(value.attempts)
    ? value.attempts.map(parseAttempt).filter((item): item is JobDiagnosticAttempt => item !== null)
    : undefined;
  const stagnation = isRecord(value.stagnation)
    ? {
        reason: textValue(value.stagnation.reason),
        level: numberValue(value.stagnation.level),
        batch_input_limit_chars: numberValue(value.stagnation.batch_input_limit_chars),
        writer_input_limit_chars: numberValue(value.stagnation.writer_input_limit_chars),
        input_items: numberValue(value.stagnation.input_items),
        output_items: numberValue(value.stagnation.output_items),
        input_chars: numberValue(value.stagnation.input_chars),
        output_chars: numberValue(value.stagnation.output_chars),
        input_writer_chars: numberValue(value.stagnation.input_writer_chars),
        output_writer_chars: numberValue(value.stagnation.output_writer_chars),
        writer_overhead_chars: numberValue(value.stagnation.writer_overhead_chars),
        evidence_context_chars: numberValue(value.stagnation.evidence_context_chars),
        item_delta: numberValue(value.stagnation.item_delta),
        char_delta: numberValue(value.stagnation.char_delta),
        top_level_batches: numberValue(value.stagnation.top_level_batches),
        model_calls: numberValue(value.stagnation.model_calls),
        model_reductions_accepted: numberValue(value.stagnation.model_reductions_accepted),
        accepted_reductions: numberValue(value.stagnation.accepted_reductions),
        accepted_reductions_total: numberValue(value.stagnation.accepted_reductions_total),
        cache_hits: numberValue(value.stagnation.cache_hits),
        singleton_passthroughs: numberValue(value.stagnation.singleton_passthroughs),
        subdivisions: numberValue(value.stagnation.subdivisions),
        outcome_counts: numberRecord(value.stagnation.outcome_counts),
        evidence_id_count_before: numberValue(value.stagnation.evidence_id_count_before),
        evidence_id_count_after: numberValue(value.stagnation.evidence_id_count_after),
        evidence_preserved: booleanValue(value.stagnation.evidence_preserved),
      }
    : undefined;
  const diagnostics: JobDiagnostics = {
    stage: textValue(value.stage),
    effective_model: effectiveModel
      && Object.values(effectiveModel).some((item) => item !== undefined)
      ? effectiveModel
      : undefined,
    context: context && Object.values(context).some((item) => item !== undefined)
      ? context
      : undefined,
    reduction: reduction && Object.values(reduction).some((item) => item !== undefined)
      ? reduction
      : undefined,
    cache: cache && Object.values(cache).some((item) => item !== undefined)
      ? cache
      : undefined,
    attempts: attempts?.length ? attempts : undefined,
    stagnation: stagnation && Object.values(stagnation).some((item) => item !== undefined)
      ? stagnation
      : undefined,
    cause: textValue(value.cause),
  };
  return Object.values(diagnostics).some((item) => item !== undefined) ? diagnostics : null;
}

export function parseJobDiagnostics(value: unknown): JobDiagnostics | null {
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (!trimmed) return null;
    try {
      return parseJobDiagnostics(JSON.parse(trimmed));
    } catch {
      return { cause: trimmed };
    }
  }
  return isRecord(value) ? parseDiagnosticsObject(value) : null;
}

export function hasJobFailureDetails(job: Job): boolean {
  return job.status === "error"
    && Boolean(job.error.trim() || parseJobDiagnostics(job.diagnostics));
}

function formatName(value: string): string {
  return value.replaceAll("_", " ");
}

function formatCount(value: number): string {
  return value.toLocaleString("en-US", { maximumFractionDigits: 0 });
}

function formatBytes(value: number): string {
  const gib = value / 1024 ** 3;
  if (gib >= 1) return `${gib.toLocaleString("en-US", { maximumFractionDigits: 1 })} GiB`;
  const mib = value / 1024 ** 2;
  return `${mib.toLocaleString("en-US", { maximumFractionDigits: 0 })} MiB`;
}

function summarizeError(error: string): string {
  const lines = error.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  if (!lines.length) return "";
  return lines[0].toLowerCase().startsWith("traceback") && lines.length > 1
    ? lines[lines.length - 1]
    : lines[0];
}

function contextSummary(diagnostics: JobDiagnostics): string | null {
  const context = diagnostics.context;
  if (!context) return null;
  const parts = [
    context.requested !== undefined && context.requested !== null
      ? `requested ${formatCount(context.requested)} tokens`
      : "",
    context.effective !== undefined && context.effective !== null
      ? `effective ${formatCount(context.effective)} tokens`
      : "",
    context.native !== undefined && context.native !== null
      ? `model native ${formatCount(context.native)} tokens`
      : "",
    context.timeout_seconds !== undefined && context.timeout_seconds !== null
      ? `timeout ${formatCount(context.timeout_seconds)} seconds`
      : "",
    context.max_output_tokens !== undefined && context.max_output_tokens !== null
      ? `maximum output ${formatCount(context.max_output_tokens)} tokens`
      : "",
  ].filter(Boolean);
  return parts.length ? parts.join("; ") : null;
}

function safetySummary(diagnostics: JobDiagnostics): string | null {
  const assessment = diagnostics.context?.safety_assessment;
  if (!assessment) return null;
  const parts = [
    assessment.tier ? formatName(assessment.tier) : "",
    assessment.message || "",
    assessment.estimated_total_bytes !== undefined
      && assessment.estimated_total_bytes !== null
      ? `estimated requirement ${formatBytes(assessment.estimated_total_bytes)}`
      : "",
    assessment.acknowledged ? "administrator override acknowledged" : "",
  ].filter(Boolean);
  return parts.length ? parts.join("; ") : null;
}

function residentTransitionSummary(diagnostics: JobDiagnostics): string | null {
  const transition = diagnostics.context?.resident_transition
    || diagnostics.context?.safety_assessment?.resident_transition;
  if (!transition) return null;
  const models = transition.replaced_models?.length
    ? transition.replaced_models
    : transition.resident_models;
  const reclaimable = [
    transition.reclaimable_ram_bytes !== undefined
      && transition.reclaimable_ram_bytes !== null
      ? `${formatBytes(transition.reclaimable_ram_bytes)} RAM`
      : "",
    transition.reclaimable_vram_bytes !== undefined
      && transition.reclaimable_vram_bytes !== null
      ? `${formatBytes(transition.reclaimable_vram_bytes)} VRAM`
      : "",
  ].filter(Boolean).join(" and ");
  const parts = [
    models?.length
      ? `${transition.required ? "replacement assessed for" : "resident capacity assessed for"} ${models.join(", ")}`
      : "",
    reclaimable ? `${reclaimable} reclaimable by Ollama` : "",
  ].filter(Boolean);
  return parts.length ? parts.join("; ") : null;
}

function reductionSummary(diagnostics: JobDiagnostics): string | null {
  const reduction = diagnostics.reduction;
  if (!reduction) return null;
  const parts = [
    reduction.purpose
      ? `${reduction.complete ? "completed " : ""}${formatName(reduction.purpose)}`
      : "",
    reduction.level !== undefined && reduction.level !== null
      ? `level ${formatCount(reduction.level)}`
      : "",
    reduction.batch !== undefined && reduction.batch !== null
      ? `batch ${formatCount(reduction.batch)}${
          reduction.batch_count !== undefined && reduction.batch_count !== null
            ? ` of ${formatCount(reduction.batch_count)}`
            : ""
        }`
      : "",
    reduction.items !== undefined && reduction.items !== null
      ? `${formatCount(reduction.items)} input items`
      : "",
    reduction.input_chars !== undefined && reduction.input_chars !== null
      ? `${formatCount(reduction.input_chars)} input characters`
      : "",
    reduction.subdivision_depth !== undefined && reduction.subdivision_depth !== null
      ? `subdivision depth ${formatCount(reduction.subdivision_depth)}`
      : "",
  ].filter(Boolean);
  return parts.length ? parts.join("; ") : null;
}

function cacheSummary(diagnostics: JobDiagnostics): string | null {
  const cache = diagnostics.cache;
  if (!cache) return null;
  const parts = [
    cache.leaf_maps_reused !== undefined && cache.leaf_maps_reused !== null
      ? `${formatCount(cache.leaf_maps_reused)} leaf maps reused`
      : "",
    cache.leaf_maps_new !== undefined && cache.leaf_maps_new !== null
      ? `${formatCount(cache.leaf_maps_new)} leaf maps generated`
      : "",
    cache.legacy_leaf_maps_reused !== undefined
      && cache.legacy_leaf_maps_reused !== null
      && cache.legacy_leaf_maps_reused > 0
      ? `${formatCount(cache.legacy_leaf_maps_reused)} legacy leaf maps reused`
      : "",
    cache.reductions_reused !== undefined && cache.reductions_reused !== null
      ? `${formatCount(cache.reductions_reused)} reductions reused`
      : "",
    cache.reductions_new !== undefined && cache.reductions_new !== null
      ? `${formatCount(cache.reductions_new)} reductions generated`
      : "",
  ].filter(Boolean);
  return parts.length ? parts.join("; ") : null;
}

function signedCount(value: number): string {
  return value > 0 ? `+${formatCount(value)}` : formatCount(value);
}

function stagnationSummary(diagnostics: JobDiagnostics): string | null {
  const stagnation = diagnostics.stagnation;
  if (!stagnation) return null;
  const acceptedTotal = stagnation.accepted_reductions_total
    ?? stagnation.accepted_reductions;
  const hasWriterRange = stagnation.input_writer_chars !== undefined
    && stagnation.input_writer_chars !== null
    && stagnation.output_writer_chars !== undefined
    && stagnation.output_writer_chars !== null;
  const hasItemRange = stagnation.input_items !== undefined
    && stagnation.input_items !== null
    && stagnation.output_items !== undefined
    && stagnation.output_items !== null;
  const hasCharacterRange = stagnation.input_chars !== undefined
    && stagnation.input_chars !== null
    && stagnation.output_chars !== undefined
    && stagnation.output_chars !== null;
  const hasEvidenceRange = stagnation.evidence_id_count_before !== undefined
    && stagnation.evidence_id_count_before !== null
    && stagnation.evidence_id_count_after !== undefined
    && stagnation.evidence_id_count_after !== null;
  const parts = [
    stagnation.reason
      ? `${formatName(stagnation.reason)}${
          stagnation.level !== undefined && stagnation.level !== null
            ? ` at level ${formatCount(stagnation.level)}`
            : ""
        }`
      : stagnation.level !== undefined && stagnation.level !== null
        ? `level ${formatCount(stagnation.level)}`
        : "",
    hasWriterRange
      ? `writer base ${formatCount(stagnation.input_writer_chars!)} to ${formatCount(
          stagnation.output_writer_chars!,
        )} characters${
          stagnation.writer_input_limit_chars !== undefined
            && stagnation.writer_input_limit_chars !== null
            ? ` against a ${formatCount(stagnation.writer_input_limit_chars)}-character limit`
            : ""
        }`
      : stagnation.output_writer_chars !== undefined && stagnation.output_writer_chars !== null
        ? `writer base ${formatCount(stagnation.output_writer_chars)} characters${
            stagnation.writer_input_limit_chars !== undefined
              && stagnation.writer_input_limit_chars !== null
              ? ` against a ${formatCount(stagnation.writer_input_limit_chars)}-character limit`
              : ""
          }`
        : "",
    stagnation.writer_overhead_chars !== undefined && stagnation.writer_overhead_chars !== null
      ? `fixed writer overhead ${formatCount(stagnation.writer_overhead_chars)} characters${
          stagnation.writer_input_limit_chars !== undefined
            && stagnation.writer_input_limit_chars !== null
            ? stagnation.writer_overhead_chars > stagnation.writer_input_limit_chars
              ? `, exceeding the ${formatCount(
                  stagnation.writer_input_limit_chars,
                )}-character limit`
              : ` within the ${formatCount(
                  stagnation.writer_input_limit_chars,
                )}-character limit`
            : ""
        }`
      : "",
    hasItemRange
      ? `items ${formatCount(stagnation.input_items!)} to ${formatCount(stagnation.output_items!)}${
          stagnation.item_delta !== undefined && stagnation.item_delta !== null
            ? ` (delta ${signedCount(stagnation.item_delta)})`
            : ""
        }`
      : "",
    hasCharacterRange
      ? `evidence ${formatCount(stagnation.input_chars!)} to ${formatCount(
          stagnation.output_chars!,
        )} characters${
          stagnation.char_delta !== undefined && stagnation.char_delta !== null
            ? ` (delta ${signedCount(stagnation.char_delta)})`
            : ""
        }`
      : "",
    stagnation.model_calls !== undefined && stagnation.model_calls !== null
      ? `${formatCount(stagnation.model_calls)} model calls`
      : "",
    stagnation.model_reductions_accepted !== undefined
      && stagnation.model_reductions_accepted !== null
      ? `${formatCount(stagnation.model_reductions_accepted)} model reductions accepted`
      : "",
    stagnation.cache_hits !== undefined && stagnation.cache_hits !== null
      ? `${formatCount(stagnation.cache_hits)} cached reductions reused`
      : "",
    (stagnation.model_reductions_accepted === undefined
      || stagnation.model_reductions_accepted === null)
      && acceptedTotal !== undefined && acceptedTotal !== null
      ? `${formatCount(acceptedTotal)} accepted reductions`
      : "",
    stagnation.singleton_passthroughs !== undefined
      && stagnation.singleton_passthroughs !== null
      ? `${formatCount(stagnation.singleton_passthroughs)} singleton passthroughs`
      : "",
    stagnation.subdivisions !== undefined && stagnation.subdivisions !== null
      ? `${formatCount(stagnation.subdivisions)} subdivisions`
      : "",
    stagnation.evidence_preserved !== undefined && stagnation.evidence_preserved !== null
      ? `evidence IDs ${stagnation.evidence_preserved ? "preserved" : "changed"}${
          hasEvidenceRange
            ? ` (${formatCount(stagnation.evidence_id_count_before!)} to ${formatCount(
                stagnation.evidence_id_count_after!,
              )})`
            : ""
        }`
      : "",
  ].filter(Boolean);
  return parts.length ? parts.join("; ") : null;
}

function attemptSummary(attempt: JobDiagnosticAttempt): string {
  const location = [
    attempt.level !== undefined && attempt.level !== null
      ? `level ${formatCount(attempt.level)}`
      : "",
    attempt.batch !== undefined && attempt.batch !== null
      ? `batch ${formatCount(attempt.batch)}`
      : "",
    attempt.depth !== undefined && attempt.depth !== null
      ? `depth ${formatCount(attempt.depth)}`
      : "",
  ].filter(Boolean).join(", ");
  const prefix = attempt.outcome ? formatName(attempt.outcome) : "attempt";
  return `${prefix}${location ? ` (${location})` : ""}${attempt.detail ? `: ${attempt.detail}` : ""}`;
}

function inventoryFailure(job: Job, diagnostics: JobDiagnostics | null): boolean {
  const values = [
    job.task,
    job.error,
    diagnostics?.stage,
    diagnostics?.reduction?.purpose,
  ].filter((value): value is string => typeof value === "string");
  return job.status === "error" && values.some((value) => {
    const normalized = value.toLowerCase().replaceAll("_", " ");
    return normalized.includes("repo inventory") || normalized.includes("repository inventory");
  });
}

export default function JobFailureDetails({ job }: { job: Job }) {
  const diagnostics = parseJobDiagnostics(job.diagnostics);
  const cause = diagnostics?.cause || summarizeError(job.error);
  const model = diagnostics?.effective_model;
  const modelSummary = [model?.provider, model?.model].filter(Boolean).join("/");
  const modelDigest = model?.digest && model.digest !== "digest_unavailable"
    ? model.digest.slice(0, 12)
    : "";
  const context = diagnostics ? contextSummary(diagnostics) : null;
  const safety = diagnostics ? safetySummary(diagnostics) : null;
  const residentTransition = diagnostics ? residentTransitionSummary(diagnostics) : null;
  const reduction = diagnostics ? reductionSummary(diagnostics) : null;
  const cache = diagnostics ? cacheSummary(diagnostics) : null;
  const stagnation = diagnostics ? stagnationSummary(diagnostics) : null;
  const technicalError = job.error.trim();
  const hasStructuredDetails = Boolean(
    diagnostics?.stage
    || modelSummary
    || context
    || safety
    || residentTransition
    || reduction
    || cache
    || stagnation
    || diagnostics?.attempts?.length,
  );
  const showTechnicalError = Boolean(
    technicalError && (hasStructuredDetails || technicalError !== cause || technicalError.includes("\n")),
  );

  return (
    <section className="job-diagnostics" aria-label="Failure diagnostics">
      <h4>Why this job failed</h4>
      {cause && <p className="job-diagnostics-cause">{cause}</p>}

      {hasStructuredDetails && (
        <dl className="job-diagnostic-facts">
          {diagnostics?.stage && (
            <>
              <dt>Stage</dt>
              <dd>{formatName(diagnostics.stage)}</dd>
            </>
          )}
          {modelSummary && (
            <>
              <dt>Effective model</dt>
              <dd>
                <code>{modelSummary}</code>
                {modelDigest ? <> (digest <code>{modelDigest}</code>)</> : null}
              </dd>
            </>
          )}
          {context && (
            <>
              <dt>Context</dt>
              <dd>{context}</dd>
            </>
          )}
          {safety && (
            <>
              <dt>Resource admission</dt>
              <dd>{safety}</dd>
            </>
          )}
          {residentTransition && (
            <>
              <dt>Ollama residency</dt>
              <dd>{residentTransition}</dd>
            </>
          )}
          {reduction && (
            <>
              <dt>Reduction</dt>
              <dd>{reduction}</dd>
            </>
          )}
          {cache && (
            <>
              <dt>Cached work</dt>
              <dd>{cache}</dd>
            </>
          )}
          {stagnation && (
            <>
              <dt>Reduction stagnation</dt>
              <dd>{stagnation}</dd>
            </>
          )}
        </dl>
      )}

      {diagnostics?.attempts && diagnostics.attempts.length > 0 && (
        <div className="job-diagnostic-attempts">
          <h5>Model attempt and adaptive subdivision history</h5>
          <ol>
            {diagnostics.attempts.map((attempt, index) => (
              <li key={`${index}-${attempt.outcome ?? "attempt"}`}>{attemptSummary(attempt)}</li>
            ))}
          </ol>
        </div>
      )}

      {inventoryFailure(job, diagnostics) && (
        <p className="job-diagnostics-skipped">
          Later repository analysis jobs were skipped because repository inventory failed.
        </p>
      )}

      {showTechnicalError && (
        <details className="job-technical-error">
          <summary>Technical error details</summary>
          <pre className="error">{technicalError}</pre>
        </details>
      )}
    </section>
  );
}
