'use client';

import * as Tooltip from '@radix-ui/react-tooltip';

/**
 * A short explanation attached to a value.
 *
 * Used sparingly. A monitoring interface that needs a tooltip on every number
 * has a labelling problem rather than a tooltip problem, so this exists for
 * the two or three places where a number is genuinely ambiguous without one.
 */
export function InfoTip({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <Tooltip.Provider delayDuration={200}>
      <Tooltip.Root>
        <Tooltip.Trigger asChild>
          <span className="cursor-default">{children}</span>
        </Tooltip.Trigger>
        <Tooltip.Portal>
          <Tooltip.Content
            side="left"
            sideOffset={6}
            className="max-w-[16rem] border border-line bg-void px-3 py-2 text-[11px] leading-relaxed text-text-secondary"
          >
            {label}
          </Tooltip.Content>
        </Tooltip.Portal>
      </Tooltip.Root>
    </Tooltip.Provider>
  );
}
