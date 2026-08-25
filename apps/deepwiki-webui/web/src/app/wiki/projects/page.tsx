'use client';

import React from 'react';
import ProcessedProjects from '@/components/ProcessedProjects';

export default function WikiProjectsPage() {
  return (
    <div className="container mx-auto p-4">
      <ProcessedProjects
        showHeader={true}
        className=""
      />
    </div>
  );
}
