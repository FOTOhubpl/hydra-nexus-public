import type { ButtonHTMLAttributes } from "react";
import { cx } from "../lib/cx";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";

const VARIANTS: Record<ButtonVariant, string> = {
  primary: "border-accent-strong bg-accent-strong text-canvas hover:bg-accent",
  secondary: "border-line-strong bg-raised text-fg hover:bg-hover",
  ghost: "border-transparent bg-transparent text-muted hover:bg-hover hover:text-fg",
  danger: "border-err/40 bg-err/10 text-err hover:bg-err/20",
};

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: "sm" | "md";
}

export function Button({ variant = "secondary", size = "md", className, type = "button", ...rest }: ButtonProps) {
  return (
    <button
      type={type}
      className={cx(
        "inline-flex shrink-0 items-center justify-center gap-1.5 rounded-[4px] border font-medium whitespace-nowrap transition-colors disabled:cursor-not-allowed disabled:border-line disabled:bg-hover disabled:text-subtle",
        size === "sm" ? "h-6 px-2 text-xs" : "h-7 px-3 text-sm",
        VARIANTS[variant],
        className,
      )}
      {...rest}
    />
  );
}
