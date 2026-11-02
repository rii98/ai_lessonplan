import type { Hook } from "@/lib/api";
import { Segmented, SeamlessText } from "@/ui";
import { HOOK_KINDS } from "../../model/ldd";
import { Issues, type SectionProps } from "../parts";
import s from "../editor.module.css";

const KIND_LABEL: Record<Hook["kind"], string> = { scenario: "Scenario", question: "Question", prediction: "Prediction", demonstration: "Demonstration" };

export function HookSection({ lesson, edit, readOnly, issues }: SectionProps) {
  const hook = lesson.engagement_hook;
  return (
    <>
      <Issues issues={issues} />
      <div style={{ marginBottom: 12 }}>
        <Segmented label="Kind of hook" value={hook.kind}
          onChange={(v) => !readOnly && edit((d) => { d.engagement_hook.kind = v; })}
          options={HOOK_KINDS.map((k) => ({ value: k, label: KIND_LABEL[k], disabled: readOnly }))} />
      </div>
      <div className={s.quote}>
        <SeamlessText className={s.hookPrompt} value={hook.prompt} readOnly={readOnly} invalid={issues.length > 0}
          aria-label="Hook" placeholder="Open with something students can wonder about — a scene, a puzzle, a prediction…"
          onValue={(v) => edit((d) => { d.engagement_hook.prompt = v; }, "hook")} />
      </div>
    </>
  );
}
