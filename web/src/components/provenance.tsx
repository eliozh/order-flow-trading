/** Provenance stamps rendered from API fields, never inferred (DESIGN §14). */

export type ProvenanceKind = 'CALC' | 'LLM' | 'STALE' | 'N/A'

const LABELS: Record<ProvenanceKind, string> = {
  CALC: 'Computed by code from market data; reproducible.',
  LLM: 'Model-generated narrative; not reproducible across models.',
  STALE: 'Data is older than its freshness budget.',
  'N/A': 'Required data does not exist.',
}

interface ProvenanceStampProps {
  kind: ProvenanceKind
  detail?: string
}

export function ProvenanceStamp({ kind, detail }: ProvenanceStampProps) {
  const className = `stamp stamp--${kind.toLowerCase().replace('/', '-')}`
  return (
    <span
      className={className}
      role="note"
      aria-label={`Provenance ${kind}. ${detail ?? LABELS[kind]}`}
      title={detail ?? LABELS[kind]}
    >
      {kind}
    </span>
  )
}
