import type { ComponentType } from "react";
import { QuizEditor, SlidesEditor, WorksheetEditor, type ArtifactEditorProps } from "./editors";
import type { Artifact, StudioKind } from "./types";

/**
 * What the studio can build. `blueprint` = it takes a question spec; `Editor`
 * edits its IR. A new artifact kind is one entry plus its editor.
 */
export const STUDIO_KINDS: Record<StudioKind, {
  label: string; noun: string; blueprint: boolean; Editor: ComponentType<ArtifactEditorProps<Artifact>>; targets: Record<string, string>;
}> = {
  quiz: { label: "Quiz", noun: "quiz", blueprint: true, Editor: QuizEditor as ComponentType<ArtifactEditorProps<Artifact>>,
    targets: { objectives: "Objectives", questions: "Questions", instructions: "Instructions", "*": "the whole quiz" } },
  worksheet: { label: "Worksheet", noun: "worksheet", blueprint: true, Editor: WorksheetEditor as ComponentType<ArtifactEditorProps<Artifact>>,
    targets: { objectives: "Objectives", tasks: "Tasks", questions: "Questions", "*": "the whole worksheet" } },
  slides: { label: "Slides", noun: "deck", blueprint: false, Editor: SlidesEditor as ComponentType<ArtifactEditorProps<Artifact>>,
    targets: { subtitle: "Subtitle", slides: "Slides", "*": "the whole deck" } },
};

export const isStudioKind = (k: string | undefined): k is StudioKind => !!k && k in STUDIO_KINDS;
