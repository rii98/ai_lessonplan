import { StatusDot } from "@/ui";
import type { Issue } from "../model/issues";
import type { Lesson } from "../model/ldd";
import { PhaseTimeline } from "./parts";
import { SECTIONS, sectionStatus } from "./sections/registry";
import s from "./editor.module.css";

export function Outline({ lesson, issues, active, onJump }: { lesson: Lesson; issues: Issue[]; active: string | null; onJump: (key: string) => void }) {
  return (
    <nav aria-label="Lesson outline">
      <div className={s.outlineTitle}>Outline</div>
      <ul className={s.outlineList}>
        {SECTIONS.map((def) => {
          const n = issues.filter((i) => i.section === def.key).length;
          return (
            <li key={def.key}>
              <button type="button" className={s.outlineLink} aria-current={active === def.key || undefined} onClick={() => onJump(def.key)}>
                <StatusDot status={sectionStatus(def, lesson, issues)} />
                <span>{def.title}</span>
                {n > 0 && <span className={s.outlineCount} style={{ color: "var(--brick)" }}>{n}</span>}
              </button>
            </li>
          );
        })}
      </ul>
      <div className={s.outlineFoot}>
        <PhaseTimeline phases={lesson.phases} duration={lesson.duration_min} legend={false} />
      </div>
    </nav>
  );
}
