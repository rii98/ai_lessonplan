import { clsx } from "clsx";
import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react";
import s from "./Button.module.css";

export type ButtonVariant = "default" | "primary" | "quiet" | "danger" | "ai";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: "md" | "sm";
  block?: boolean;
  /** Shows a spinner, keeps the label (no layout jump) and blocks clicks. */
  busy?: boolean;
  icon?: ReactNode;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "default", size = "md", block, busy, icon, className, children, disabled, type = "button", ...rest }, ref,
) {
  return (
    <button ref={ref} type={type} disabled={disabled || busy} aria-busy={busy || undefined}
      className={clsx(s.btn, variant !== "default" && s[variant], size === "sm" && s.sm, block && s.block, className)} {...rest}>
      {busy ? <span className={s.spin} aria-hidden /> : icon}
      {children}
    </button>
  );
});

export interface IconButtonProps extends Omit<ButtonProps, "icon" | "children"> {
  /** Accessible name — icon buttons always need one. */
  label: string;
  children: ReactNode;
}

export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
  { label, children, className, variant = "quiet", ...rest }, ref,
) {
  return (
    <Button ref={ref} variant={variant} aria-label={label} title={label} className={clsx(s.icon, className)} {...rest}>
      {children}
    </Button>
  );
});
