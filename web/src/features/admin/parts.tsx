import type { ReactNode } from "react";
import { StatusDot } from "@/ui";
import s from "./admin.module.css";

export const Kpi = ({ value, label, ok }: { value: ReactNode; label: ReactNode; ok?: boolean }) => (
  <div className={s.kpi}>
    <div className={s.kpiV}>{value ?? "—"}</div>
    <div className={s.kpiL}>{ok !== undefined && <StatusDot status={ok ? "ok" : "error"} />}{label}</div>
  </div>
);
export const Kpis = ({ children }: { children: ReactNode }) => <div className={s.kpis}>{children}</div>;
export const Json = ({ value }: { value: unknown }) => <pre className={s.json}>{JSON.stringify(value, null, 2)}</pre>;
export const Cmd = ({ children }: { children: string }) => <code className={s.cmd}>{children}</code>;

export interface Column<T> { label: string; cell: (r: T) => ReactNode }

export function Table<T>({ columns, rows, onRow, empty = "Nothing here yet.", rowKey }: {
  columns: Column<T>[]; rows: T[]; onRow?: (r: T) => void; empty?: string; rowKey: (r: T, i: number) => string;
}) {
  return (
    <div className={s.tableWrap}>
      <table className={s.table}>
        <thead><tr>{columns.map((c) => <th key={c.label} scope="col">{c.label}</th>)}</tr></thead>
        <tbody>
          {!rows.length && <tr><td colSpan={columns.length} style={{ color: "var(--ink-3)" }}>{empty}</td></tr>}
          {rows.map((r, i) => (
            <tr key={rowKey(r, i)} className={onRow ? s.click : undefined} onClick={onRow ? () => onRow(r) : undefined}
              tabIndex={onRow ? 0 : undefined} onKeyDown={onRow ? (e) => { if (e.key === "Enter") onRow(r); } : undefined}>
              {columns.map((c) => <td key={c.label}>{c.cell(r)}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
