/** POST /grounding/preview — contract gap: untyped dict on the server. */
export interface GroundingChunk { heading_path?: string | null; chapter?: string | null; chunk_index?: number | null; score: number; text?: string | null }
export interface GroundingDocument { source: string; chunks: GroundingChunk[] }
export interface GroundingCollection { name: string; authoritative?: boolean; count: number; documents: GroundingDocument[] }
export interface GroundingTree { grounded: boolean; sources?: string[]; collections: GroundingCollection[] }
export interface GroundingQuery { topic: string; grade?: number | null; subject?: string | null; framework?: string | null }
