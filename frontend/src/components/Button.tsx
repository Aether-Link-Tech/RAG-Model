import type { ButtonHTMLAttributes, ReactNode } from 'react';

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: 'primary' | 'ghost' | 'glass' | 'danger';
  wide?: boolean;
  loading?: boolean;
  loadingText?: string;
  children: ReactNode;
}

export default function Button({
  variant = 'primary',
  wide = false,
  loading = false,
  loadingText,
  className = '',
  disabled,
  children,
  ...rest
}: ButtonProps) {
  const classes = ['button', `button--${variant}`, wide ? 'button--wide' : '', className].join(' ');

  return (
    <button className={classes} disabled={disabled || loading} {...rest}>
      {loading ? (
        <>
          <span className="spinner" />
          {loadingText && <span>{loadingText}</span>}
        </>
      ) : (
        children
      )}
    </button>
  );
}
