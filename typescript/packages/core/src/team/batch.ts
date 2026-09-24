import type { EventDraft } from "../store/admit";
import { uuidv7 } from "../store/encode";

/**
 * Mints the event id of the event a batch appends at `seq`. The runtime's is a UUIDv7 from the
 * append's clock; a test injects a deterministic one to compare ids with the op vectors.
 */
export type Mint = (seq: number, now: number) => string;

export const MINT: Mint = (_seq, now) => uuidv7(now);

/**
 * The drafts of one team append, each with its event id minted before the append: later events
 * of the same batch name earlier ones (a monitor's registering event, a mail's causal event), and
 * a runtime mail's id is its message_sent's event id.
 */
export class Batch {
  readonly drafts: EventDraft[] = [];
  readonly #head: number;
  readonly #now: number;
  readonly #mint: Mint;
  /** The next draft's id, once asked for: it is the id that draft gets. */
  #next: string | undefined;

  /** A batch appended after the committed event at `head`. */
  constructor(head: number, now: number, mint: Mint = MINT) {
    this.#head = head;
    this.#now = now;
    this.#mint = mint;
  }

  /** The event id the next added draft gets. */
  nextId(): string {
    this.#next ??= this.#mint(this.#head + this.drafts.length + 1, this.#now);
    return this.#next;
  }

  /**
   * The mail this batch already takes (a receipt, a task's input, a refusal): the index moves
   * those rows only when the batch commits, so a later read in the same batch skips them.
   */
  taken(): ReadonlySet<string> {
    return new Set(
      this.drafts.flatMap((d) =>
        (d.type === "message_received" || d.type === "mail_refused") &&
        d.data.mail_id !== undefined
          ? [d.data.mail_id]
          : d.type === "user_input" && d.data.mail_id !== undefined
            ? [d.data.mail_id]
            : [],
      ),
    );
  }

  /** Adds a draft and returns its event id: its own, or one minted now. */
  add(draft: EventDraft): string {
    const id = draft.event_id ?? this.nextId();
    this.#next = undefined;
    this.drafts.push({ ...draft, event_id: id });
    return id;
  }
}
