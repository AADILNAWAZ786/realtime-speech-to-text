import { Compass } from "lucide-react";

import { ButtonLink } from "../components/ui/Button";
import { EmptyState } from "../components/ui/ErrorState";

export function NotFoundPage() {
  return (
    <div className="flex min-h-[60vh] items-center justify-center">
      <EmptyState
        icon={<Compass className="size-6" aria-hidden="true" />}
        title="Page not found"
        description="The page you are looking for does not exist."
        action={<ButtonLink to="/">Go to dashboard</ButtonLink>}
      />
    </div>
  );
}
