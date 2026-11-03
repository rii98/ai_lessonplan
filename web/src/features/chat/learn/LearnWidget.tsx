import { useState } from "react";
import { Icon, RichText } from "@/ui";
import { WIDGETS } from "../artifacts/quiz/widgets";
import type { PlayQuestion } from "../artifacts/quiz/types";
import type { LearnFeedback, LearnWidget as Widget } from "../types";
import s from "./learn.module.css";

const SURE = ["Guessing", "Unsure", "Maybe", "Fairly sure", "Certain"];

/**
 * An inline learn-mode check inside the dialogue. The input widgets are the quiz's
 * (same play-view shape, same server grading); this adds the card, an optional
 * confidence rating (metacognition) and the feedback once graded.
 *
 * Three looks, by design weight:
 *  - **open** (the student's turn): the focal point of the page — raised card, the
 *    largest prompt in the thread.
 *  - **waiting** (streaming in, or superseded): the same card, unraised.
 *  - **graded**: collapses to a compact result. The options are history now; what
 *    matters is the outcome and the correct answer. A miss is marigold ("look
 *    here"), not brick — a wrong answer in a lesson is not an error.
 */
export function LearnWidget({ widget, feedback, busy, onAnswer }: {
  widget: Widget; feedback: LearnFeedback | null; busy: boolean;
  onAnswer?: (response: unknown, confidence?: number) => void;
}) {
  const [sure, setSure] = useState<number | undefined>();
  if (feedback) return <GradedCheck widget={widget} feedback={feedback} />;

  const Input = WIDGETS[widget.widget] ?? WIDGETS.text!;
  const open = !!onAnswer;
  return (
    <section className={s.widget} data-open={open || undefined} data-purpose={widget.purpose} aria-label={widget.label}>
      <div className={s.widgetHead}>
        <span className={s.widgetLabel}>{widget.purpose === "challenge" ? <Icon.Star size={13} /> : <Icon.ListChecks size={13} />}{widget.label}</span>
        {open && <span className={s.yourTurn}>Your turn</span>}
      </div>
      <div className={s.widgetPrompt}><RichText text={widget.prompt} /></div>
      <Input q={widget as PlayQuestion} result={null} busy={busy || !open} submit={(r) => onAnswer?.(r, sure)} />
      {widget.ask_confidence && open && (
        <div className={s.sure} role="group" aria-label="How sure are you?">
          <span>How sure are you?</span>
          {SURE.map((l, i) => (
            <button key={l} type="button" aria-pressed={sure === i + 1} data-on={sure === i + 1 || undefined} onClick={() => setSure(sure === i + 1 ? undefined : i + 1)}>{l}</button>
          ))}
        </div>
      )}
    </section>
  );
}

function GradedCheck({ widget, feedback }: { widget: Widget; feedback: LearnFeedback }) {
  const tone = feedback.correct ? "ok" : feedback.correct === false ? "miss" : "done";
  const verdict = tone === "ok" ? "Correct" : tone === "miss" ? "Not quite" : "Noted";
  return (
    <section className={s.graded} data-tone={tone} aria-label={`${widget.label}: ${verdict}`}>
      <div className={s.gradedHead}>
        <span className={s.verdict}>
          {tone === "ok" ? <Icon.CheckCircle2 size={15} /> : tone === "miss" ? <Icon.RotateCcw size={14} /> : <Icon.Check size={14} />}
          {verdict}
        </span>
        <span className={s.gradedKind}>{widget.label}</span>
      </div>
      <div className={s.gradedPrompt}><RichText text={widget.prompt} /></div>
      {feedback.correct === false && feedback.expected && (
        <div className={s.gradedAnswer}><span>Answer</span><RichText text={feedback.expected} className={s.gradedAnswerText} /></div>
      )}
      {feedback.explanation && <div className={s.gradedNote}><RichText text={feedback.explanation} /></div>}
      {feedback.reveal && <div className={s.reveal}><Icon.Lightbulb size={14} /> {feedback.reveal}</div>}
      {feedback.feedback && <div className={s.gradedNote}>{feedback.feedback}</div>}
    </section>
  );
}
