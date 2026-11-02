import { lazy, type ComponentType, type LazyExoticComponent } from "react";
import type { ArtifactSummary } from "../types";

export interface ArtifactRendererProps {
  id: string;
  /** The artifact's summary changed (attempts, best score) — refresh its card. */
  onSummary: (s: ArtifactSummary) => void;
  /** It spawned a new artifact (e.g. "retry the missed ones"). */
  onNew: (s: ArtifactSummary) => void;
  onDeleted: (id: string) => void;
}

/**
 * artifact kind → renderer (loaded on first open). A flashcard deck or study plan
 * adds one line here; the card, canvas, persistence and history are shared.
 */
export const ARTIFACT_RENDERERS: Record<string, LazyExoticComponent<ComponentType<ArtifactRendererProps>>> = {
  quiz: lazy(() => import("./quiz/QuizArtifact").then((m) => ({ default: m.QuizArtifact }))),
};
