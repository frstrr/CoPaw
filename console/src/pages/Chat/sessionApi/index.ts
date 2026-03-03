import {
  IAgentScopeRuntimeWebUISession,
  IAgentScopeRuntimeWebUISessionAPI,
  IAgentScopeRuntimeWebUIMessage,
} from "@agentscope-ai/chat";
import api, { type ChatSpec, type Message } from "../../../api";

interface CustomWindow extends Window {
  currentSessionId?: string;
  currentUserId?: string;
  currentChannel?: string;
}

declare const window: CustomWindow;

// ---------------------------------------------------------------------------
// Local helper types
// ---------------------------------------------------------------------------

/** A single item inside a message's content array. */
interface ContentItem {
  type: string;
  text?: string;
  [key: string]: unknown;
}

/** A backend message after role-normalisation (output of toOutputMessage). */
interface OutputMessage extends Omit<Message, "role"> {
  role: string;
  metadata: null;
  sequence_number?: number;
}

/**
 * Extended session carrying extra fields that the library type does not define
 * but our backend / window globals require.
 */
interface ExtendedSession extends IAgentScopeRuntimeWebUISession {
  sessionId: string;
  userId: string;
  channel: string;
  meta: Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// Message conversion: backend flat messages → card-based UI format
// ---------------------------------------------------------------------------

function generateId(): string {
  return `${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
}

/**
 * Extract plain text from a message's content array.
 */
function extractTextFromContent(content: unknown): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return String(content || "");
  return (content as ContentItem[])
    .filter((c) => c.type === "text")
    .map((c) => c.text || "")
    .filter(Boolean)
    .join("\n");
}

/**
 * Convert a backend message to a response output message.
 * - Maps system + plugin_call_output → role "tool"
 * - Strips metadata (not used in card rendering)
 */
function toOutputMessage(msg: Message): OutputMessage {
  let role = msg.role;
  if (msg.type === "plugin_call_output" && role === "system") {
    role = "tool";
  }
  return {
    ...msg,
    role,
    metadata: null,
  };
}

/**
 * Build a user card (AgentScopeRuntimeRequestCard) from a user message.
 */
function buildUserCard(msg: Message): IAgentScopeRuntimeWebUIMessage {
  const text = extractTextFromContent(msg.content);
  return {
    id: (msg.id as string) || generateId(),
    role: "user",
    cards: [
      {
        code: "AgentScopeRuntimeRequestCard",
        data: {
          input: [
            {
              role: "user",
              type: "message",
              content: [{ type: "text", text, status: "created" }],
            },
          ],
        },
      },
    ],
  };
}

/**
 * Build an assistant response card (AgentScopeRuntimeResponseCard)
 * wrapping a group of consecutive non-user output messages.
 */
function buildResponseCard(
  outputMessages: OutputMessage[],
): IAgentScopeRuntimeWebUIMessage {
  const now = Math.floor(Date.now() / 1000);
  const maxSeq = outputMessages.reduce(
    (max: number, m: OutputMessage) => Math.max(max, m.sequence_number || 0),
    0,
  );
  return {
    id: generateId(),
    role: "assistant",
    cards: [
      {
        code: "AgentScopeRuntimeResponseCard",
        data: {
          id: `response_${generateId()}`,
          output: outputMessages,
          object: "response",
          status: "completed",
          created_at: now,
          sequence_number: maxSeq + 1,
          error: null,
          completed_at: now,
          usage: null,
        },
      },
    ],
    msgStatus: "finished",
  };
}

/**
 * Convert flat backend messages into the card-based format expected by
 * the @agentscope-ai/chat component.
 *
 * - User messages → AgentScopeRuntimeRequestCard
 * - Consecutive non-user messages (assistant / system / tool) → grouped
 *   into a single AgentScopeRuntimeResponseCard with all messages in
 *   the `output` array, supporting plugin_call & plugin_call_output.
 */
function convertMessages(
  messages: Message[],
): IAgentScopeRuntimeWebUIMessage[] {
  const result: IAgentScopeRuntimeWebUIMessage[] = [];
  let i = 0;

  while (i < messages.length) {
    const msg = messages[i];

    if (msg.role === "user") {
      result.push(buildUserCard(msg));
      i++;
    } else {
      // Group consecutive non-user messages into one response card
      const outputMsgs: OutputMessage[] = [];
      while (i < messages.length && messages[i].role !== "user") {
        outputMsgs.push(toOutputMessage(messages[i]));
        i++;
      }
      if (outputMsgs.length > 0) {
        result.push(buildResponseCard(outputMsgs));
      }
    }
  }

  return result;
}

function chatSpecToSession(chat: ChatSpec): ExtendedSession {
  return {
    id: chat.id,
    name: (chat as ChatSpec & { name?: string }).name || "New Chat",
    sessionId: chat.session_id,
    userId: chat.user_id,
    channel: chat.channel,
    messages: [],
    meta: chat.meta || {},
  } as ExtendedSession;
}

class SessionApi implements IAgentScopeRuntimeWebUISessionAPI {
  private lsKey: string;
  private sessionList: IAgentScopeRuntimeWebUISession[];
  private fetchPromise: Promise<IAgentScopeRuntimeWebUISession[]> | null = null;
  private lastFetchTime: number = 0;
  private cacheTimeout: number = 5000;

  private sessionCache: Map<
    string,
    { session: IAgentScopeRuntimeWebUISession; timestamp: number }
  > = new Map();
  private sessionFetchPromises: Map<
    string,
    Promise<IAgentScopeRuntimeWebUISession>
  > = new Map();
  private sessionCacheTimeout: number = 5000;

  /**
   * Stores the most recently synced live messages for a session (from
   * syncSessionMessages / updateSession calls). Used to restore the
   * in-progress UI state when the user switches back to a session whose
   * task has not yet completed. No expiry — superseded only when the
   * backend confirms a more complete message list.
   */
  private liveSessionMessages: Map<
    string,
    IAgentScopeRuntimeWebUIMessage[]
  > = new Map();

  constructor() {
    this.lsKey = "agent-scope-runtime-webui-sessions";
    this.sessionList = [];
  }

  private createEmptySession(sessionId: string): ExtendedSession {
    window.currentSessionId = sessionId;
    window.currentUserId = "default";
    window.currentChannel = "console";

    return {
      id: sessionId,
      name: "New Chat",
      sessionId: sessionId,
      userId: "default",
      channel: "console",
      messages: [],
      meta: {},
    } as ExtendedSession;
  }

  private updateWindowVariables(session: ExtendedSession): void {
    window.currentSessionId = session.sessionId || "";
    window.currentUserId = session.userId || "default";
    window.currentChannel = session.channel || "console";
  }

  private getLocalSession(sessionId: string): IAgentScopeRuntimeWebUISession {
    // Prefer live messages (in-progress state) over the empty sessionList entry
    const live = this.liveSessionMessages.get(sessionId);
    const localSession = this.sessionList.find((s) => s.id === sessionId);
    if (localSession) {
      this.updateWindowVariables(localSession as ExtendedSession);
      if (live && live.length > 0) {
        return { ...localSession, messages: live };
      }
      return localSession;
    }
    return this.createEmptySession(sessionId);
  }

  async getSessionList() {
    if (this.fetchPromise) {
      return this.fetchPromise;
    }

    const now = Date.now();
    if (
      this.sessionList.length > 0 &&
      now - this.lastFetchTime < this.cacheTimeout
    ) {
      return [...this.sessionList];
    }

    this.fetchPromise = this.fetchSessionListFromBackend();

    try {
      const result = await this.fetchPromise;
      return result;
    } finally {
      this.fetchPromise = null;
    }
  }

  private async fetchSessionListFromBackend(): Promise<
    IAgentScopeRuntimeWebUISession[]
  > {
    try {
      const chats = await api.listChats();
      const validChats = chats.filter(
        (chat) => chat.id && chat.id !== "undefined" && chat.id !== "null",
      );
      this.sessionList = validChats.map(chatSpecToSession).reverse();
      localStorage.setItem(this.lsKey, JSON.stringify(this.sessionList));
      this.lastFetchTime = Date.now();
      return [...this.sessionList];
    } catch (error) {
      this.sessionList = JSON.parse(localStorage.getItem(this.lsKey) || "[]");
      return [...this.sessionList];
    }
  }

  async getSession(sessionId: string) {
    try {
      if (!sessionId || sessionId === "undefined" || sessionId === "null") {
        return this.createEmptySession(`temp-${Date.now()}`);
      }

      const isLocalTimestampId = /^\d+$/.test(sessionId);

      if (isLocalTimestampId) {
        return this.getLocalSession(sessionId);
      }

      const cached = this.sessionCache.get(sessionId);
      const now = Date.now();

      if (cached && now - cached.timestamp < this.sessionCacheTimeout) {
        this.updateWindowVariables(cached.session as ExtendedSession);
        // If we have a more complete live state, overlay it onto the cached session
        const live = this.liveSessionMessages.get(sessionId);
        if (live && live.length > cached.session.messages.length) {
          return { ...cached.session, messages: live };
        }
        return cached.session;
      }

      const existingPromise = this.sessionFetchPromises.get(sessionId);
      if (existingPromise) {
        return existingPromise;
      }

      const fetchPromise = this.fetchSessionFromBackend(sessionId);
      this.sessionFetchPromises.set(sessionId, fetchPromise);

      try {
        const result = await fetchPromise;
        return result;
      } finally {
        this.sessionFetchPromises.delete(sessionId);
      }
    } catch (error) {
      const fallbackSession = this.sessionList.find(
        (session) => session.id === sessionId,
      );
      if (fallbackSession) {
        return fallbackSession;
      }
      return this.createEmptySession(sessionId);
    }
  }

  private async fetchSessionFromBackend(
    sessionId: string,
  ): Promise<IAgentScopeRuntimeWebUISession> {
    const chatHistory = await api.getChat(sessionId);
    const backendMessages = convertMessages(chatHistory.messages || []);

    const chatSpec = this.sessionList.find((s) => s.id === sessionId) as
      | ExtendedSession
      | undefined;

    // If we have live messages (task in progress) with more content than what
    // the backend returned, keep the live state so the user sees their pending
    // message rather than an empty / pre-task history.
    const live = this.liveSessionMessages.get(sessionId);
    let messages: IAgentScopeRuntimeWebUIMessage[];
    if (live && live.length > backendMessages.length) {
      // Backend hasn't caught up yet — task still in progress
      messages = live;
    } else {
      // Backend is at least as complete as live state: use backend truth
      messages = backendMessages;
      this.liveSessionMessages.delete(sessionId);
    }

    const session = {
      id: sessionId,
      name: chatSpec?.name || sessionId,
      sessionId: chatSpec?.sessionId || sessionId,
      userId: chatSpec?.userId || "default",
      channel: chatSpec?.channel || "console",
      messages,
      meta: chatSpec?.meta || {},
    } as ExtendedSession;

    this.updateWindowVariables(session);

    this.sessionCache.set(sessionId, {
      session,
      timestamp: Date.now(),
    });

    return session;
  }

  async updateSession(session: Partial<IAgentScopeRuntimeWebUISession>) {
    // Persist the live message state BEFORE clearing it from the localStorage
    // snapshot.  This is what gets returned by getSession when the user
    // switches back to this session while a task is still in progress.
    if (session.id && Array.isArray(session.messages) && session.messages.length > 0) {
      this.liveSessionMessages.set(session.id, [...session.messages]);
    }

    session.messages = [];
    const index = this.sessionList.findIndex((item) => item.id === session.id);
    if (index > -1) {
      this.sessionList[index] = {
        ...this.sessionList[index],
        ...session,
      };
      localStorage.setItem(this.lsKey, JSON.stringify(this.sessionList));
    }

    return [...this.sessionList];
  }

  async createSession(session: Partial<IAgentScopeRuntimeWebUISession>) {
    session.id = Date.now().toString();

    const extendedSession = {
      ...session,
      sessionId: session.id,
      userId: "default",
      channel: "console",
    } as ExtendedSession;

    this.updateWindowVariables(extendedSession);

    this.sessionList.unshift(extendedSession as IAgentScopeRuntimeWebUISession);
    localStorage.setItem(this.lsKey, JSON.stringify(this.sessionList));
    this.lastFetchTime = Date.now();
    return [...this.sessionList];
  }

  async removeSession(session: Partial<IAgentScopeRuntimeWebUISession>) {
    try {
      if (!session.id) {
        return [...this.sessionList];
      }

      const sessionId = session.id;

      await api.deleteChat(sessionId);

      this.sessionList = this.sessionList.filter(
        (item) => item.id !== sessionId,
      );
      this.liveSessionMessages.delete(sessionId);
      this.sessionCache.delete(sessionId);

      localStorage.setItem(this.lsKey, JSON.stringify(this.sessionList));
      this.lastFetchTime = Date.now();
      return [...this.sessionList];
    } catch (error) {
      if (session.id) {
        this.sessionList = this.sessionList.filter(
          (item) => item.id !== session.id,
        );
        this.liveSessionMessages.delete(session.id);
        this.sessionCache.delete(session.id);
        localStorage.setItem(this.lsKey, JSON.stringify(this.sessionList));
        this.lastFetchTime = Date.now();
      }
      return [...this.sessionList];
    }
  }
}

export default new SessionApi();
