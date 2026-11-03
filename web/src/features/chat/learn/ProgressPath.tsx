import { Badge, Button, Icon } from "@/ui";
import type { LearnState } from "../types";
import s from "./learn.module.css";

/**
 * The lesson's stepping stones as a thin stepper: a segment per objective, with
 * only the current one's name at full strength. Done steps recede (a filled bar and
 * a check); the eye lands on "where am I now", not on three equal pills.
 */
export function ProgressPath({ state, busy, onPause, onResume, onEnd }: {
  state: LearnState; busy: boolean; onPause: () => void; onResume: () => void; onEnd: () => void;
}) {
  const paused = state.status === "paused";
  return (
    <div className={s.path} aria-label={`Lesson: ${state.topic}`}>
      <div className={s.pathHead}>
        <span className={s.pathTitle}><Icon.GraduationCap size={16} /> {state.topic}</span>
        <span className={s.pathCount}>{state.mastered} of {state.total} done</span>
        {state.ungrounded && <Badge tone="marigold">Not from your textbook</Badge>}
        {paused && <Badge>Paused</Badge>}
        <span style={{ flex: 1 }} />
        {paused
          ? <Button size="sm" variant="primary" icon={<Icon.Play size={14} />} busy={busy} onClick={onResume}>Continue lesson</Button>
          : <Button size="sm" variant="quiet" icon={<Icon.Pause size={14} />} disabled={busy} onClick={onPause}>Break</Button>}
        <Button size="sm" variant="quiet" icon={<Icon.Flag size={14} />} disabled={busy} onClick={onEnd}>End</Button>
      </div>
      <ol className={s.steps}>
        {state.objectives.map((o, i) => {
          const st = o.status === "skipped" ? "mastered" : o.status;
          const current = o.id === state.cursor && !paused;
          return (
            <li key={o.id} className={s.step} data-status={st} data-current={current || undefined} aria-current={current ? "step" : undefined}
              title={st === "left" ? `${o.title} — we'll come back to this` : o.title}>
              <span className={s.seg} aria-hidden />
              <span className={s.stepLabel}>
                {st === "mastered" ? <Icon.Check size={12} aria-label="done" /> : o.early ? <Icon.Star size={11} aria-label="shown early" /> : <span className={s.stepN}>{i + 1}</span>}
                <span className={s.stepText}>{o.title}</span>
              </span>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
