import { Badge, Button, Icon } from "@/ui";
import type { LearnState } from "../types";
import s from "./learn.module.css";

/** The lesson's stepping stones — where the student is, what's done, what they showed early. */
export function ProgressPath({ state, busy, onPause, onResume, onEnd }: {
  state: LearnState; busy: boolean; onPause: () => void; onResume: () => void; onEnd: () => void;
}) {
  const paused = state.status === "paused";
  return (
    <div className={s.path} aria-label={`Lesson: ${state.topic}`}>
      <div className={s.pathHead}>
        <span className={s.pathTitle}><Icon.GraduationCap size={16} /> {state.topic}</span>
        <span className={s.pathCount}>{state.mastered}/{state.total}</span>
        {state.ungrounded && <Badge tone="marigold">Not from your textbook</Badge>}
        <span style={{ flex: 1 }} />
        {paused
          ? <Button size="sm" variant="primary" icon={<Icon.Play size={14} />} busy={busy} onClick={onResume}>Continue lesson</Button>
          : <Button size="sm" variant="quiet" icon={<Icon.Pause size={14} />} disabled={busy} onClick={onPause}>Break</Button>}
        <Button size="sm" variant="quiet" icon={<Icon.Flag size={14} />} disabled={busy} onClick={onEnd}>End</Button>
      </div>
      <ol className={s.stones}>
        {state.objectives.map((o, i) => {
          const st = o.status === "skipped" ? "mastered" : o.status;
          return (
            <li key={o.id} className={s.stone} data-status={st} data-current={o.id === state.cursor && !paused || undefined}
              title={st === "left" ? `${o.title} — we'll come back to this` : o.title}>
              <span className={s.dot}>{st === "mastered" ? <Icon.Check size={12} /> : o.early ? <Icon.Star size={11} /> : i + 1}</span>
              <span className={s.stoneText}>{o.title}</span>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
