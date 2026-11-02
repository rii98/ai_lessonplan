import * as M from "@radix-ui/react-dropdown-menu";
import * as P from "@radix-ui/react-popover";
import { clsx } from "clsx";
import type { ReactNode } from "react";
import s from "./Overlay.module.css";

export interface MenuItem {
  label: ReactNode;
  icon?: ReactNode;
  hint?: ReactNode;
  onSelect: () => void;
  danger?: boolean;
  disabled?: boolean;
}
export type MenuEntry = MenuItem | "separator" | { heading: string };

/** A dropdown of actions. `trigger` must be a single focusable element (a Button). */
export function Menu({ trigger, items, align = "end" }: { trigger: ReactNode; items: MenuEntry[]; align?: "start" | "end" }) {
  return (
    <M.Root modal={false}>
      <M.Trigger asChild>{trigger}</M.Trigger>
      <M.Portal>
        <M.Content className={s.menu} align={align} sideOffset={6} collisionPadding={8}>
          {items.map((it, i) =>
            it === "separator" ? <M.Separator key={i} className={s.sep} />
            : "heading" in it ? <M.Label key={i} className={s.menuLabel}>{it.heading}</M.Label>
            : (
              <M.Item key={i} className={clsx(s.item, it.danger && s.itemDanger)} disabled={it.disabled} onSelect={it.onSelect}>
                {it.icon}{it.label}{it.hint && <span className={s.itemHint}>{it.hint}</span>}
              </M.Item>
            ))}
        </M.Content>
      </M.Portal>
    </M.Root>
  );
}

export function Popover({ trigger, children, open, onOpenChange, align = "start" }: {
  trigger: ReactNode; children: ReactNode; open?: boolean; onOpenChange?: (o: boolean) => void; align?: "start" | "center" | "end";
}) {
  return (
    <P.Root open={open} onOpenChange={onOpenChange}>
      <P.Trigger asChild>{trigger}</P.Trigger>
      <P.Portal>
        <P.Content className={s.popover} align={align} sideOffset={6} collisionPadding={8}>{children}</P.Content>
      </P.Portal>
    </P.Root>
  );
}
