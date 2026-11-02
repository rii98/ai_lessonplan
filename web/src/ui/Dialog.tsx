import * as D from "@radix-ui/react-dialog";
import { clsx } from "clsx";
import { useState, type ReactNode } from "react";
import { Button } from "./Button";
import { Field, TextInput } from "./Field";
import { X } from "./icons";
import s from "./Overlay.module.css";

interface BaseProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
  footer?: ReactNode;
}

function Header({ title, description }: Pick<BaseProps, "title" | "description">) {
  return (
    <div className={s.head}>
      <div>
        <D.Title className={s.title}>{title}</D.Title>
        {description ? <D.Description className={s.desc}>{description}</D.Description>
          : <D.Description className="visually-hidden">{typeof title === "string" ? title : "Dialog"}</D.Description>}
      </div>
      <D.Close asChild>
        <button type="button" aria-label="Close" style={{ border: 0, background: "none", cursor: "pointer", color: "var(--ink-3)", padding: 6 }}>
          <X size={18} />
        </button>
      </D.Close>
    </div>
  );
}

/** Centred modal (a bottom sheet on phones). */
export function Dialog({ open, onOpenChange, title, description, children, footer, wide }: BaseProps & { wide?: boolean }) {
  return (
    <D.Root open={open} onOpenChange={onOpenChange}>
      <D.Portal>
        <D.Overlay className={s.scrim} />
        <D.Content className={clsx(s.dialog, wide && s.wide)}>
          <Header title={title} description={description} />
          {children && <div className={s.body}>{children}</div>}
          {footer && <div className={s.foot}>{footer}</div>}
        </D.Content>
      </D.Portal>
    </D.Root>
  );
}

/** Side panel on desktop, bottom sheet on phones — for inspectors and references. */
export function Sheet({ open, onOpenChange, title, description, children, footer }: BaseProps) {
  return (
    <D.Root open={open} onOpenChange={onOpenChange}>
      <D.Portal>
        <D.Overlay className={s.scrim} />
        <D.Content className={s.sheet}>
          <Header title={title} description={description} />
          <div className={s.body}>{children}</div>
          {footer && <div className={s.foot}>{footer}</div>}
        </D.Content>
      </D.Portal>
    </D.Root>
  );
}

export interface ConfirmProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: ReactNode;
  body?: ReactNode;
  confirmLabel: string;
  danger?: boolean;
  busy?: boolean;
  /** For irreversible actions: the user must type this word back. */
  typeToConfirm?: string;
  onConfirm: () => void;
}

/** Replaces window.confirm/prompt: themable, accessible, never blocks the page. */
export function ConfirmDialog({ open, onOpenChange, title, body, confirmLabel, danger, busy, typeToConfirm, onConfirm }: ConfirmProps) {
  const [typed, setTyped] = useState("");
  const ok = !typeToConfirm || typed.trim() === typeToConfirm;
  return (
    <Dialog open={open} onOpenChange={(o) => { if (!o) setTyped(""); onOpenChange(o); }} title={title}
      footer={<>
        <Button variant="quiet" onClick={() => onOpenChange(false)}>Cancel</Button>
        <Button variant={danger ? "danger" : "primary"} busy={busy} disabled={!ok} onClick={onConfirm}>{confirmLabel}</Button>
      </>}>
      {body}
      {typeToConfirm && (
        <div style={{ marginTop: 16 }}>
          <Field label={<>Type <code>{typeToConfirm}</code> to confirm</>}>
            <TextInput value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" autoFocus />
          </Field>
        </div>
      )}
    </Dialog>
  );
}
