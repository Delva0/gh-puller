import { NextResponse } from 'next/server';

// The target backend server base URL, derived from environment variable or defaulted.
const TARGET_SERVER_BASE_URL = process.env.SERVER_BASE_URL || 'http://localhost:8001';

// The registry response is the UI's source of truth for target selectors.
export async function GET() {
  try {
    const targetUrl = `${TARGET_SERVER_BASE_URL}/generators/config`;
    const backendResponse = await fetch(targetUrl, {
      method: 'GET',
      headers: { 'Accept': 'application/json' },
    });

    if (!backendResponse.ok) {
      return NextResponse.json(
        { error: `Backend service responded with status: ${backendResponse.status}` },
        { status: backendResponse.status }
      );
    }
    return NextResponse.json(await backendResponse.json());
  } catch (error) {
    console.error('Error fetching generators config:', error);
    return new NextResponse(JSON.stringify({ error: error }), {
      status: 500,
      headers: { 'Content-Type': 'application/json' },
    });
  }
}

export function OPTIONS() {
  return new NextResponse(null, {
    status: 204,
    headers: {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET',
      'Access-Control-Allow-Headers': 'Content-Type, Authorization',
    },
  });
}
