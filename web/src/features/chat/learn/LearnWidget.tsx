import { useState } from "react";
import { Icon, RichText } from "@/ui";
import { WIDGETS } from "../artifacts/quiz/widgets";
import type { Graded, PlayQuestion } from "../artifacts/quiz/types";
import type { LearnFeedback, LearnWidget as Widget } from "../types";
import s from "./learn.module.css";

const SURE = ["Guessing", "Unsure", "Maybe", "Fairly sure", "Certain"];

/**
 * An inline learn-mode check inside the dialogue. The input widgets are the quiz's
 * (same play-view shape, same server grading); this adds the card, an optional
 * confidence rating (metacognition) and the feedback once graded.
 */
export function LearnWidget({ widget, feedback, busy, onAnswer }: {
  widget: Widget; feedback: LearnFeedback | null; busy: boolean;
  onAnswer?: (response: unknown, confidence?: number) => void;
}) {
  const [sure, setSure] = useState<number | undefined>();
  const Input = WIDGETS[widget.widget] ?? WIDGETS.text!;
  const graded: Graded | null = feedback ? { correct: feedback.correct, score: feedback.score, expected: feedback.expected,
    explanation: feedback.explanation, feedback: feedback.feedback } : null;
  const locked = !!feedback || !onAnswer;
  return (
    <div className={s.widget} data-purpose={widget.purpose} data-state={feedback ? (feedback.correct ? "ok" : feedback.correct === false ? "no" : "done") : undefined}>
      <div className={s.widgetHead}>
        <span className={s.widgetLabel}>{widget.purpose === "challenge" ? <Icon.Star size={13} /> : <Icon.ListChecks size={13} />}{widget.label}</span>
      </div>
      <div className={s.widgetPrompt}><RichText text={widget.prompt} /></div>
      {widget.ask_confidence && !locked && (
        <div className={s.sure} role="group" aria-label="How sure are you?">
          <span>How sure are you?</span>
          {SURE.map((l, i) => (
            <button key={l} type="button" aria-pressed={sure === i + 1} data-on={sure === i + 1 || undefined} onClick={() => setSure(sure === i + 1 ? undefined : i + 1)}>{l}</button>
          ))}
        </div>
      )}
      <Input q={widget as PlayQuestion} result={graded} busy={busy || locked} submit={(r) => onAnswer?.(r, sure)} />
      {feedback && (
        <div className={s.feedback}>
          <strong>{feedback.correct ? "Correct!" : feedback.correct === false ? "Not quite." : "Thanks!"}</strong>
          {feedback.correct === false && feedback.expected && <span> The answer: <b>{feedback.expected}</b>.</span>}
          {feedback.explanation && <div className={s.muted}>{feedback.explanation}</div>}
          {feedback.reveal && <div className={s.reveal}><Icon.Lightbulb size={14} /> {feedback.reveal}</div>}
          {feedback.feedback && <div className={s.muted}>{feedback.feedback}</div>}
        </div>
      )}
    </div>
  );
}
