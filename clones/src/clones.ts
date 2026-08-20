export interface CloneEntry {
  /** Origin slug + hash, e.g. "example-com-3f2a1b9c" */
  siteKey: string;
  /** Human-readable label for the workspace index */
  label: string;
  /** Original URL this route was cloned from */
  source: string;
  /** Local route, e.g. "/example-com-3f2a1b9c/docs/intro" */
  route: string;
  /** ISO date the clone was built */
  clonedAt: string;
}

/**
 * Every page built by /clone-website. The skill appends an entry here after a
 * clone builds clean; the workspace index at src/app/page.tsx renders the list.
 */
export const CLONES: CloneEntry[] = [];
