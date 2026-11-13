import React from "react";

interface HashLinkProps {
  readonly href: string;
  readonly children: React.ReactNode;
  readonly className?: string;
  readonly title?: string;
}

export const HashLink: React.FC<HashLinkProps> = ({ href, children, className, title }) => (
  <a
    className={[
      "inline-flex items-center gap-1 rounded-md text-sm font-medium text-[#1f2a44] underline decoration-[#d39b3b]/50 underline-offset-3 transition hover:text-[#d39b3b] focus:outline-none focus-visible:ring-2 focus-visible:ring-[#d39b3b] focus-visible:ring-offset-2",
      className,
    ]
      .filter(Boolean)
      .join(" ")}
    href={href}
    title={title}
  >
    {children}
  </a>
);
