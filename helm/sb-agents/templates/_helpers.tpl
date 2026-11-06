{{- define "sb-agents.image" -}}
{{ .Values.image.repository }}:{{ .Values.image.tag }}
{{- end }}

{{- define "sb-agents.labels" -}}
app.kubernetes.io/part-of: sb-agents
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/* Pod spec fields every sb-agents Deployment shares. */}}
{{- define "sb-agents.podCommon" -}}
# No Docker-style service env vars: a Service named mt-agent would inject
# MT_AGENT_PORT=tcp://..., which is mt-agent's own port setting.
enableServiceLinks: false
priorityClassName: {{ .Values.priorityClass.name }}
imagePullSecrets:
- name: sb-agents-ghcr
securityContext:
  runAsNonRoot: true
  runAsUser: 10001
{{- end }}

{{- define "sb-agents.commonEnv" -}}
- {name: SB_ENV, value: {{ .Values.env | quote }}}
- name: ANTHROPIC_API_KEY
  valueFrom: {secretKeyRef: {name: sb-agents, key: ANTHROPIC_API_KEY}}
{{- end }}

{{- define "sb-agents.specialists" -}}
http://docs-agent.{{ .Release.Namespace }}.svc.cluster.local:8101,http://mt-agent.{{ .Release.Namespace }}.svc.cluster.local:8102
{{- end }}
