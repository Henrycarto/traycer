'use client';

import * as ScrollArea from '@radix-ui/react-scroll-area';

/**
 * Scroll container with a scrollbar that matches the rest of the interface.
 *
 * Native overflow works fine, but the platform scrollbar on Windows draws a
 * light grey track that cuts a bright line down a near black panel. Radix
 * gives a styled bar without taking over the scroll behaviour.
 */
export function ScrollPane({
  children,
  maxHeight,
  className = '',
}: {
  children: React.ReactNode;
  maxHeight: number;
  className?: string;
}) {
  return (
    <ScrollArea.Root className={`overflow-hidden ${className}`} style={{ maxHeight }}>
      <ScrollArea.Viewport className="h-full w-full" style={{ maxHeight }}>
        {children}
      </ScrollArea.Viewport>
      <ScrollArea.Scrollbar
        orientation="vertical"
        className="flex w-2 touch-none select-none bg-void p-0"
      >
        <ScrollArea.Thumb className="flex-1 bg-line" />
      </ScrollArea.Scrollbar>
    </ScrollArea.Root>
  );
}
