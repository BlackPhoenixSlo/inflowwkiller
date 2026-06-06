"use client";

import { Suspense } from "react";

import Landing from "@/components/auth/Landing";

export default function LoginPage() {
  // useSearchParams() inside Landing needs a Suspense boundary at the
  // page level for Next 16's streaming SSR.
  return (
    <Suspense fallback={null}>
      <Landing />
    </Suspense>
  );
}
