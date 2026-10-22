/* A reviewed request to the current CLI's own sub-agent support. No I/O. */
(function (root, factory) {
  'use strict';
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.SessionholicDelegation = api;
})(typeof globalThis === 'object' ? globalThis : this, function () {
  'use strict';
  const KINDS = Object.freeze({ review: '검토', research: '추가 조사', validation: '검증' });
  function clean(value, max) {
    return typeof value === 'string' ? Array.from(value.replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, '').trim()).slice(0, max).join('') : '';
  }
  function buildRequest(value) {
    if (!value || !Object.hasOwn(KINDS, value.kind)) throw new Error('맡길 작업 종류를 선택해 주세요.');
    const task = clean(value.task, 4000), completion = clean(value.completion, 1000);
    if (!task || !completion) throw new Error('맡길 일과 완료 기준을 적어 주세요.');
    const materials = clean(value.materials, 6000);
    return ['현재 작업의 일부를 네이티브 하위 에이전트에 맡겨 주세요.',
      `작업 종류: ${KINDS[value.kind]}`, `맡길 일:\n${task}`,
      `원본 수정 요청: ${value.allowEdits === true ? '이 요청의 범위에서만 허용합니다.' : '허용하지 않습니다. 검토·조사·검증 결과만 돌려주세요.'}`,
      `전달할 자료:\n${materials || '추가 발췌 없음. 이 요청의 범위만 전달하세요.'}`,
      `완료 기준:\n${completion}`,
      '지원되는 네이티브 하위 에이전트 기능을 사용하세요. 지원 여부나 권한을 확인할 수 없으면 실행하지 말고 제한을 알려주세요. 하위 작업을 실제로 실행하지 않고 실행했다고 보고하지 마세요.',
      '전체 대화·인증값·첨부를 자동으로 넘기지 마세요. 명시한 자료와 작업 범위만 전달하고 기존 작업이나 다른 계정의 실행을 중단하지 마세요.',
      '결과는 이 부모 대화에 돌려주세요. 수행 내용, 원본 하위 대화 식별자(확인된 경우), 변경 파일/결과물, 실제 검증과 미확인 사항을 구분해 알려주세요.'].join('\n\n');
  }
  return Object.freeze({ KINDS, buildRequest });
});
