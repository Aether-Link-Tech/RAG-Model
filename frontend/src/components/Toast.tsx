// Small notification stack (top-right) with enter/exit animations.
import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from 'react';
import { CircleAlert, CircleCheck, Info, X } from 'lucide-react';

type ToastTone = 'success' | 'error' | 'info';

interface ToastItem {
  id: number;
  tone: ToastTone;
  title: string;
  message?: string;
  leaving?: boolean;
}

interface ToastApi {
  show: (tone: ToastTone, title: string, message?: string) => void;
  success: (title: string, message?: string) => void;
  error: (title: string, message?: string) => void;
  info: (title: string, message?: string) => void;
}

const ToastContext = createContext<ToastApi | null>(null);

const ICONS = { success: CircleCheck, error: CircleAlert, info: Info };

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const nextId = useRef(1);

  const dismiss = useCallback((id: number) => {
    setToasts((list) => list.map((t) => (t.id === id ? { ...t, leaving: true } : t)));
    setTimeout(() => setToasts((list) => list.filter((t) => t.id !== id)), 260);
  }, []);

  const show = useCallback(
    (tone: ToastTone, title: string, message?: string) => {
      const id = nextId.current++;
      setToasts((list) => [...list.slice(-3), { id, tone, title, message }]);
      setTimeout(() => dismiss(id), tone === 'error' ? 6500 : 4000);
    },
    [dismiss],
  );

  const api = useMemo<ToastApi>(
    () => ({
      show,
      success: (title, message) => show('success', title, message),
      error: (title, message) => show('error', title, message),
      info: (title, message) => show('info', title, message),
    }),
    [show],
  );

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div className="toast-stack" aria-live="polite">
        {toasts.map((toast) => {
          const Icon = ICONS[toast.tone];
          return (
            <div key={toast.id} role="status" className={`toast toast--${toast.tone}${toast.leaving ? ' toast--leaving' : ''}`}>
              <Icon size={19} className="toast__icon" />
              <div className="toast__body">
                <strong>{toast.title}</strong>
                {toast.message && <span>{toast.message}</span>}
              </div>
              <button type="button" className="icon-button toast__close" aria-label="Dismiss" onClick={() => dismiss(toast.id)}>
                <X size={15} />
              </button>
            </div>
          );
        })}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastApi {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error('useToast must be used inside <ToastProvider>');
  return ctx;
}
