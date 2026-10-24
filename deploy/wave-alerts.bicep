// Opt-in only. No action group is created and no message is sent by this module.
param appName string
param location string
param workspaceId string
param enabled bool = false
var base = '''
ContainerAppConsoleLogs_CL
| where ContainerAppName_s == '${appName}-wave-recovery'
| extend d = parse_json(extract(@'processing telemetry (\{.*\})', 1, Log_s))
'''
var rules = [
  {
    name: 'wave-stalled'
    query: '${base}\n| where tostring(d.event) == \'wave_recovery\'\n| summarize arg_max(TimeGenerated, *) by tostring(d.library_id), tostring(d.wave_id)\n| where (tostring(d.state) == \'collecting\' and todouble(d.no_progress_seconds) > 86400) or (tostring(d.state) == \'waiting_predecessor\' and todouble(d.state_age_seconds) > 7200) or ((toint(d.outstanding_leases) > 0 or toint(d.outstanding_members) > 0) and todouble(d.barrier_wait_seconds) > 1800) or (toint(d.handoff_pending_count) > 0 and todouble(d.handoff_age_seconds) > 900) or todouble(d.stage_no_progress_seconds) > 1800 or toint(d.dispatch_failed_count) > 0'
    operator: 'GreaterThan'
  }
  {
    name: 'wave-recovery-failed'
    query: '${base}\n| where tostring(d.event) == \'wave_recovery_error\' or (tostring(d.event) == \'wave_recovery_run\' and toint(d.errors) > 0)'
    operator: 'GreaterThan'
  }
  {
    name: 'wave-recovery-missing'
    query: '${base}\n| where tostring(d.event) == \'wave_recovery_run\' and d.disabled == false'
    operator: 'LessThanOrEqual'
  }
]
resource alerts 'Microsoft.Insights/scheduledQueryRules@2023-12-01' = [for rule in rules: {
  name: '${appName}-${rule.name}'
  location: location
  kind: 'LogAlert'
  properties: {
    displayName: '${appName}: ${rule.name}'
    description: 'Wave safety signal; inspect the durable wave, stage lease and outbox before repair.'
    enabled: enabled
    severity: 2
    evaluationFrequency: 'PT5M'
    windowSize: 'PT30M'
    scopes: [workspaceId]
    autoMitigate: true
    criteria: {
      allOf: [{
        query: rule.query
        timeAggregation: 'Count'
        operator: rule.operator
        threshold: 0
        failingPeriods: { numberOfEvaluationPeriods: 1, minFailingPeriodsToAlert: 1 }
      }]
    }
    actions: { actionGroups: [] }
  }
}]
