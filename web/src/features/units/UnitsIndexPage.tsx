import { Link } from "react-router-dom";
import { describeError } from "@/lib/api";
import { relativeTime } from "@/lib/format";
import { Button, EmptyState, ErrorState, Icon, Page, PageHeader, Skeleton } from "@/ui";
import { useUnitList } from "./api";
import s from "./units.module.css";

/** `/units` — your units, and the way into planning a new one. */
export function UnitsIndexPage() {
  const q = useUnitList();
  return (
    <Page>
      <PageHeader title="Units" lede="A chapter taught over several days, planned as one arc."
        actions={<Link to="/units/new"><Button variant="primary" icon={<Icon.Plus size={16} />}>Plan a unit</Button></Link>} />
      {q.isPending ? (
        <div className={s.grid}>{[0, 1, 2].map((i) => <div key={i} className={s.card}><Skeleton h={20} w="70%" /><Skeleton w="40%" /></div>)}</div>
      ) : q.error ? (
        <ErrorState error={describeError(q.error)} onRetry={() => q.refetch()} />
      ) : !q.data.length ? (
        <EmptyState icon={<Icon.CalendarRange size={40} />} title="No units yet"
          action={<Link to="/units/new"><Button variant="primary">Plan your first unit</Button></Link>}>
          Plan a chapter across a week or two. You shape the arc, then every day is written to fit it.
        </EmptyState>
      ) : (
        <div className={s.grid}>
          {q.data.map((d) => (
            <Link key={d.id} to={`/units/${d.id}`} className={s.card}>
              <span className={s.cardTitle}>{d.title || "Untitled unit"}</span>
              <span className={s.meta}>{[d.grade && `Grade ${d.grade}`, d.subject].filter(Boolean).join(" · ")}<span>·</span>{relativeTime(d.updated_at)}</span>
            </Link>
          ))}
        </div>
      )}
    </Page>
  );
}
