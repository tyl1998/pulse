import { describe, expect, expectTypeOf, test, vi } from 'vitest';
import {
  RuntimeApiClient,
  RuntimeApiError,
  runtimeEventStreamUrl,
  type PulseRuntimeApiSchemas,
} from './runtime.generated';

const response = (body: unknown, status = 200) =>
  ({
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers({ 'content-type': 'application/json' }),
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  }) as Response;

function harness(body: unknown = {}) {
  const calls: { url: string; init: RequestInit }[] = [];
  const fetchMock = vi.fn<typeof fetch>((input, init = {}) => {
    calls.push({ url: String(input), init });
    return Promise.resolve(response(body));
  });
  const client = new RuntimeApiClient({
    baseUrl: 'http://127.0.0.1:1234',
    token: 'secret',
    fetch: fetchMock,
  });
  return { calls, fetchMock, client };
}

describe('generated Runtime API client', () => {
  test('carries the bearer token and serializes a run request body', async () => {
    const { calls, client } = harness({ run_id: 'r1' });
    const request: PulseRuntimeApiSchemas.RunRequest = {
      project_id: 'demo',
      case_id: 'cart',
      device: 'serial',
    };
    await client.startRun(request);
    expect(calls).toHaveLength(1);
    expect(calls[0]?.url).toBe('http://127.0.0.1:1234/runs');
    expect(calls[0]?.init.method).toBe('POST');
    expect(calls[0]?.init.headers).toMatchObject({ Authorization: 'Bearer secret' });
    expect(calls[0]?.init.body).toBe(JSON.stringify(request));
  });

  test('encodes path parameters and query values', async () => {
    const { calls, client } = harness({ events: [] });
    await client.getCase('project id', 'case/one');
    await client.getEvents({ since: 7, run_id: 'run', limit: 20 });
    expect(calls[0]?.url).toBe('http://127.0.0.1:1234/projects/project%20id/cases/case%2Fone');
    expect(calls[1]?.url).toBe('http://127.0.0.1:1234/events?since=7&run_id=run&limit=20');
  });

  test('reads a typed fixed-target installation check with bearer auth and no request body', async () => {
    const check: PulseRuntimeApiSchemas.TargetAppResponse = {
      serial: '192.0.2.10:5555',
      package: 'com.pulse.target',
      status: 'installed',
      version_name: null,
      version_code: null,
      observed_at_ms: 1_000,
      diagnostic: { code: 'target_app_version_unavailable', guidance: null },
    };
    const { calls, client } = harness(check);
    await expect(client.getTargetApp(check.serial)).resolves.toEqual(check);
    expect(calls[0]?.url).toBe('http://127.0.0.1:1234/devices/192.0.2.10%3A5555/target-app');
    expect(calls[0]?.init).toMatchObject({
      method: 'GET',
      headers: { Authorization: 'Bearer secret' },
    });
    expect(calls[0]?.init.body).toBeUndefined();
  });

  test('locate types expose the existing target, four-state result, and source-local evidence', () => {
    expectTypeOf<RuntimeApiClient['locate']>().parameters.toEqualTypeOf<
      [serial: string, body: PulseRuntimeApiSchemas.LocateRequest]
    >();
    expectTypeOf<RuntimeApiClient['locate']>().returns.toEqualTypeOf<
      Promise<PulseRuntimeApiSchemas.LocateResult>
    >();
    expectTypeOf<PulseRuntimeApiSchemas.LocateRequest['target']>().toEqualTypeOf<
      PulseRuntimeApiSchemas.LocatorRef | PulseRuntimeApiSchemas.InlineLocator
    >();
    expectTypeOf<PulseRuntimeApiSchemas.LocateResult['status']>().toEqualTypeOf<
      'resolved' | 'ambiguous' | 'not_found' | 'indeterminate'
    >();
    expectTypeOf<PulseRuntimeApiSchemas.LocateResult['attempts']>().toEqualTypeOf<
      PulseRuntimeApiSchemas.LocateAttempt[]
    >();
    expectTypeOf<
      PulseRuntimeApiSchemas.LocateResult['error']
    >().toEqualTypeOf<PulseRuntimeApiSchemas.LocateError | null>();
    expectTypeOf<
      PulseRuntimeApiSchemas.LocateResult['observation']['sources']
    >().toEqualTypeOf<PulseRuntimeApiSchemas.LocateObservationSources>();
    expectTypeOf<PulseRuntimeApiSchemas.LocateMatch['states']['checked']>().toEqualTypeOf<
      boolean | null
    >();
    expectTypeOf<
      PulseRuntimeApiSchemas.LocateResult['action_target']
    >().toEqualTypeOf<PulseRuntimeApiSchemas.LocateActionTarget | null>();
    expectTypeOf<PulseRuntimeApiSchemas.LocateActionTarget['source']>().toEqualTypeOf<'tree'>();
  });

  test.each(['inline', 'reference'])(
    'sends a typed %s locate body and preserves the full result',
    async (targetKind) => {
      const locator: PulseRuntimeApiSchemas.Locator = {
        candidates: [
          { by: 'res_id', value: 'com.pulse.target:id/submit' },
          { by: 'text', value: 'Submit' },
        ],
        expect: { unique: true, visible: true },
      };
      const request: PulseRuntimeApiSchemas.LocateRequest = {
        serial: '192.0.2.10:5555',
        session_id: '12345678-1234-4234-8234-123456789abc',
        request_id: '87654321-4321-4321-8321-cba987654321',
        expected_package: 'com.pulse.target',
        target: targetKind === 'inline' ? { locator } : { locator_ref: 'submit' },
        locators: { submit: locator },
        resources: { marker: { png_base64: 'iVBORw0KGgo=' } },
        display_id: 0,
        timeout_ms: 15_000,
        image_threshold: 0.999,
      };
      function source(
        name: PulseRuntimeApiSchemas.LocateObservationSource['source'],
        coordinateSpace: PulseRuntimeApiSchemas.LocateSourceCoordinates['coordinate_space'],
        requested: boolean,
      ): PulseRuntimeApiSchemas.LocateObservationSource {
        return {
          source: name,
          requested,
          capability: 'available',
          status: requested ? 'available' : 'unavailable',
          error: requested ? null : { code: 'not_requested' },
          time: {
            clock: 'android_uptime_ms',
            calibrated: false,
            started_at_uptime_ms: requested ? 100 : null,
            finished_at_uptime_ms: requested ? 110 : null,
          },
          coordinates: {
            coordinate_space: coordinateSpace,
            requested_display_id: 0,
            display_id: requested ? 0 : null,
            width: null,
            height: null,
            original_width: null,
            original_height: null,
            scale_x: null,
            scale_y: null,
            rotation: null,
            ...(coordinateSpace === 'screenshot_pixels'
              ? {
                  to_original_bitmap: {
                    status: 'unavailable',
                    error: { code: 'original_dimensions_unknown' },
                  },
                  to_display: { status: 'unavailable', error: { code: 'rotation_unverified' } },
                }
              : {}),
          },
          scope: {
            kind: coordinateSpace === 'display_pixels' ? 'active_window' : 'default_display',
            display_id: requested ? 0 : null,
            window_id: requested ? 1 : null,
            target_app_verified: null,
          },
          completeness: {
            status: requested ? 'partial' : 'unavailable',
            reasons: requested ? ['non_atomic_snapshot', 'rotation_unverified'] : ['not_requested'],
          },
        };
      }
      const result: PulseRuntimeApiSchemas.LocateResult = {
        contract: 'pulse-runtime-locator-draft',
        serial: request.serial,
        session_id: request.session_id,
        request_id: request.request_id,
        observation_id: request.request_id,
        status: 'resolved',
        error: null,
        candidate_index: 0,
        candidate_by: 'res_id',
        match_count: 1,
        matches: [
          {
            source: 'tree',
            coordinate_space: 'display_pixels',
            display_id: 0,
            node_path: [0],
            block_index: null,
            bounds: { left: 0, top: 0, right: 40, bottom: 20 },
            visible: true,
            visibility_evidence: 'native_state',
            states: {
              enabled: true,
              checked: null,
              checkable: false,
              clickable: true,
              editable: false,
              focused: false,
              showing_hint_text: false,
              selected: false,
              visible: true,
              password: null,
              long_clickable: null,
              scrollable: null,
            },
            score: null,
          },
        ],
        query_complete: true,
        attempts: [{ index: 0, by: 'res_id', status: 'resolved', match_count: 1, error: null }],
        point: null,
        action_target: {
          observation_id: request.request_id,
          source: 'tree',
          coordinate_space: 'display_pixels',
          display_id: 0,
          expected_package: request.expected_package,
          node_path: [0],
        },
        action_target_error: null,
        business_success: null,
        automatic_replay: false,
        observation: {
          status: 'completed',
          error: null,
          agent_lifecycle_id: '12345678-1234-4234-8234-123456789abc',
          framing: {
            protocol_version: 1,
            validated: true,
            source_data_validated: true,
            terminal_received: true,
            eof_received: true,
          },
          runtime_time: {
            started_at_epoch_ms: 1_000,
            finished_at_epoch_ms: 1_010,
            duration_ms: 10,
          },
          completeness: { status: 'partial', reasons: ['non_atomic_snapshot'] },
          revision_start: 1,
          revision_end: 1,
          action_context: {
            status: 'available',
            source: 'adb_input_viewport',
            geometry: { display_id: 0, width: 40, height: 20, rotation: 0 },
            error: null,
            atomic_with_observation: false,
          },
          requested: { tree: true, screenshot: false, ocr: false },
          requested_display_id: 0,
          atomic_snapshot: false,
          sources: {
            tree: source('accessibility', 'display_pixels', true),
            screenshot: source('accessibility_screenshot', 'screenshot_pixels', false),
            ocr: source('mlkit_chinese', 'screenshot_pixels', false),
          },
        },
      };
      const { calls, client } = harness(result);
      const located = await client.locate(request.serial, request);
      expectTypeOf(located).toEqualTypeOf<PulseRuntimeApiSchemas.LocateResult>();
      expect(located).toEqual(result);
      expect(calls).toHaveLength(1);
      expect(calls[0]?.url).toBe('http://127.0.0.1:1234/devices/192.0.2.10%3A5555/agent/locate');
      expect(calls[0]?.init).toMatchObject({
        method: 'POST',
        headers: { Authorization: 'Bearer secret', 'Content-Type': 'application/json' },
        body: JSON.stringify(request),
      });
    },
  );

  test('returns text for non-JSON responses instead of parsing them as JSON', async () => {
    const fetchMock = vi.fn<typeof fetch>(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        headers: new Headers({ 'content-type': 'text/html; charset=utf-8' }),
        text: () => Promise.resolve('<html>report</html>'),
        json: () => {
          throw new Error('should not parse html as json');
        },
      } as unknown as Response),
    );
    const client = new RuntimeApiClient({
      baseUrl: 'http://127.0.0.1:1',
      token: 't',
      fetch: fetchMock,
    });
    await expect(client.getRunReportHtml('r1')).resolves.toBe('<html>report</html>');
  });

  test('surfaces non-2xx responses as RuntimeApiError without hiding the body', async () => {
    const fetchMock = vi.fn<typeof fetch>(() =>
      Promise.resolve(response({ error: 'run_not_active' }, 409)),
    );
    const client = new RuntimeApiClient({
      baseUrl: 'http://127.0.0.1:1',
      token: 't',
      fetch: fetchMock,
    });
    await expect(client.pauseRun('r1')).rejects.toMatchObject({
      name: 'RuntimeApiError',
      status: 409,
      detail: { error: 'run_not_active' },
    });
    await expect(client.pauseRun('r1')).rejects.toBeInstanceOf(RuntimeApiError);
  });

  test.each([
    {
      name: 'plain-text 500',
      status: 500,
      contentType: 'text/plain',
      body: 'Internal Server Error',
      detail: 'Internal Server Error',
    },
    {
      name: 'empty body',
      status: 502,
      contentType: 'application/json',
      body: '',
      detail: '',
    },
    {
      name: 'malformed JSON',
      status: 500,
      contentType: 'application/json',
      body: '{"error":',
      detail: '{"error":',
    },
    {
      name: 'valid JSON error',
      status: 409,
      contentType: 'application/json',
      body: '{"error":"run_not_active","detail":"cannot pause"}',
      detail: { error: 'run_not_active', detail: 'cannot pause' },
    },
  ])('reads a real Response body once for $name', async ({ status, contentType, body, detail }) => {
    const errorResponse = new Response(body, {
      status,
      headers: { 'content-type': contentType },
    });
    const text = vi.spyOn(errorResponse, 'text');
    const json = vi.spyOn(errorResponse, 'json');
    const client = new RuntimeApiClient({
      baseUrl: 'http://127.0.0.1:1',
      token: 't',
      fetch: vi.fn<typeof fetch>().mockResolvedValue(errorResponse),
    });
    const request = client.pauseRun('r1');

    await expect(request).rejects.toBeInstanceOf(RuntimeApiError);
    await expect(request).rejects.toMatchObject({ status, detail });
    expect(text).toHaveBeenCalledOnce();
    expect(json).not.toHaveBeenCalled();
    expect(errorResponse.bodyUsed).toBe(true);
  });

  test('builds an authenticated event stream URL with backfill parameters', () => {
    const url = new URL(
      runtimeEventStreamUrl({
        baseUrl: 'http://127.0.0.1:9',
        token: 'tok en',
        since: 42,
        runId: '12345678-1234-4234-8234-123456789abc',
      }),
    );
    expect(url.protocol).toBe('ws:');
    expect(url.pathname).toBe('/ws/events');
    expect(url.searchParams.get('token')).toBe('tok en');
    expect(url.searchParams.get('since')).toBe('42');
    expect(url.searchParams.get('run_id')).toBe('12345678-1234-4234-8234-123456789abc');
  });
});
