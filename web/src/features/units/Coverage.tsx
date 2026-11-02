import type { CoverageReport, UnitPlan } from "@/lib/api";
import { Banner, Icon } from "@/ui";
import { coverageGaps, topicLabel } from "./gaps";
import s from "./units.module.css";

/** The chapter's syllabus against the arc — gaps turn green as you cover them. */
export function Coverage({ report, plan }: { report: CoverageReport | null; plan: UnitPlan }) {
  const topics = report?.topics ?? [];
  if (!topics.length) return null;
  const gaps = coverageGaps(topics, plan);
  const covered = topics.length - gaps.size;
  return (
    <div className={s.coverage}>
      <Banner tone={gaps.size ? "warning" : "success"} title={gaps.size ? `${covered} of ${topics.length} chapter topics covered` : `Covers all ${topics.length} chapter topics`}>
        {gaps.size ? "Topics in amber may be missing — extend a day or add one before writing the lessons." : report?.source ? `Checked against ${report.source}.` : null}
      </Banner>
      <div className={s.covChips}>
        {topics.map((t) => {
          const gap = gaps.has(t);
          return (
            <span key={t} className={`${s.cov} ${gap ? s.covGap : s.covHas}`} title={t}>
              {gap ? <Icon.Circle size={10} /> : <Icon.Check size={11} />}{topicLabel(t)}
            </span>
          );
        })}
      </div>
    </div>
  );
}
