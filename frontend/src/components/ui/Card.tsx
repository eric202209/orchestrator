import { ReactNode } from 'react';

interface CardProps {
  children: ReactNode;
  className?: string;
  onClick?: () => void;
  hoverable?: boolean;
}

export function Card({ children, className = '', onClick, hoverable = false }: CardProps) {
  return (
    <div
      className={`rounded-lg border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] p-4 ${
        hoverable
          ? 'cursor-pointer transition-all duration-150 hover:border-[color:var(--oc-border)] hover:bg-[color:var(--oc-surface-raised)] hover:-translate-y-px'
          : ''
      } ${className}`}
      onClick={onClick}
    >
      {children}
    </div>
  );
}

export default Card;
