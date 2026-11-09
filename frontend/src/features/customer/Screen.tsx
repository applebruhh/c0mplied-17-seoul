import type { ReactNode } from "react";

export function Screen({ title, onBack, action, step, footer, children }: {
  title: ReactNode; onBack?: () => void; action?: ReactNode; step?: string; footer?: ReactNode; children: ReactNode;
}) {
  return (
    <div className="flex min-h-dvh flex-col bg-canvas">
      <header className="sticky top-0 z-10 flex items-center gap-2 bg-canvas/95 px-4 pb-3 pt-4 backdrop-blur">
        {onBack && (
          <button type="button" onClick={onBack} aria-label="뒤로" className="-ml-1 rounded-lg p-1.5 text-xl leading-none hover:bg-black/5">‹</button>
        )}
        <h1 className="flex-1 text-[17px] font-bold">{title}</h1>
        {step && <span className="rounded-full bg-mint px-2 py-0.5 text-[11px] font-semibold text-mint-text">{step}</span>}
        {action}
      </header>
      <main className="flex-1 px-4 pb-6">{children}</main>
      {footer && <footer className="sticky bottom-0 bg-canvas/95 px-4 pb-6 pt-3 backdrop-blur">{footer}</footer>}
    </div>
  );
}
