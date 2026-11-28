import { useEffect, type RefObject } from 'react';

const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(', ');

function isVisible(el: HTMLElement): boolean {
  if (el.hidden || el.closest('[hidden], [inert]')) return false;
  if (typeof window !== 'undefined' && window.getComputedStyle(el).visibility === 'hidden') return false;
  // In jsdom, getClientRects() is empty for everything, so only check rects when layout exists.
  const style = typeof window !== 'undefined' ? window.getComputedStyle(el) : null;
  if (style && style.display === 'none') return false;
  return true;
}

function getFocusable(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(
    (el) => !el.hasAttribute('disabled') && el.getAttribute('aria-hidden') !== 'true' && isVisible(el),
  );
}

/**
 * Traps keyboard Tab navigation inside a dialog/drawer, handles Escape,
 * and restores focus to the trigger element on close.
 */
export function useFocusTrap(
  containerRef: RefObject<HTMLElement | null>,
  active: boolean,
  onClose: () => void,
  canClose = true,
) {
  useEffect(() => {
    if (!active) return;
    const previous = document.activeElement as HTMLElement | null;
    const container = containerRef.current;

    if (container && !container.contains(document.activeElement)) {
      const focusables = getFocusable(container);
      (focusables[0] ?? container).focus();
    }

    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape' && canClose) {
        event.stopPropagation();
        onClose();
        return;
      }

      if (event.key !== 'Tab' || !container) return;
      const focusables = getFocusable(container);
      if (focusables.length === 0) {
        event.preventDefault();
        container.focus();
        return;
      }

      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      const current = document.activeElement as HTMLElement | null;

      if (event.shiftKey) {
        if (!current || current === first || !container.contains(current)) {
          event.preventDefault();
          last.focus();
        }
      } else if (!current || current === last || !container.contains(current)) {
        event.preventDefault();
        first.focus();
      }
    }

    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('keydown', onKeyDown);
      previous?.focus?.();
    };
  }, [containerRef, active, onClose, canClose]);
}
