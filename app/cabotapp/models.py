from django.db import models
from polymorphic import PolymorphicModel
from django.db.models import F
from django.contrib.admin.models import User

from jenkins import get_job_status
from .alert import send_alert
from .calendar import get_events
from .graphite import parse_metric
from .alert import send_alert
from datetime import datetime, timedelta
from django.utils import timezone

import json
import re
import time

import requests
from celery.utils.log import get_task_logger

logger = get_task_logger(__name__)

CHECK_TYPES = (
  ('>', 'Greater than'),
  ('>=', 'Greater than or equal'),
  ('<', 'Less than'),
  ('<=', 'Less than or equal'),
  ('==', 'Equal to'),
)

def serialize_recent_results(recent_results):
  if not recent_results:
    return ''
  def result_to_value(result):
    if result.succeeded:
      return '1'
    else:
      return '-1'
  vals = [result_to_value(r) for r in recent_results]
  vals.reverse()
  return ','.join(vals)

def calculate_debounced_passing(recent_results, debounce=0):
  """
  `debounce` is the number of previous failures we need (not including this)
  to mark a search as passing or failing
  Returns:
    True if passing given debounce factor
    False if failing
  """
  if not recent_results:
    return True
  debounce_window = recent_results[:debounce+1]
  for r in debounce_window:
    if r.succeeded:
      return True
  return False


class Service(models.Model):
  PASSING_STATUS = 'PASSING'
  WARNING_STATUS = 'WARNING'
  ERROR_STATUS = 'ERROR'
  CRITICAL_STATUS = 'CRITICAL'

  CALCULATED_PASSING_STATUS = 'passing'
  CALCULATED_INTERMITTENT_STATUS = 'intermittent'
  CALCULATED_FAILING_STATUS = 'failing'

  STATUSES = (
    (CALCULATED_PASSING_STATUS, CALCULATED_PASSING_STATUS),
    (CALCULATED_INTERMITTENT_STATUS, CALCULATED_INTERMITTENT_STATUS),
    (CALCULATED_FAILING_STATUS, CALCULATED_FAILING_STATUS),
  )

  IMPORTANCES = (
    (WARNING_STATUS, 'Warning'),
    (ERROR_STATUS, 'Error'),
    (CRITICAL_STATUS, 'Critical'),
  )

  name = models.TextField()
  url = models.TextField(
    blank=True,
    help_text="URL of service."
  )
  users_to_notify = models.ManyToManyField(
    User,
    blank=True,
    help_text='Users who should receive alerts.',
  )
  status_checks = models.ManyToManyField(
    'StatusCheck',
    blank=True,
    help_text='Checks used to calculate service status.',
  )
  last_alert_sent = models.DateTimeField(
    null=True,
    blank=True,
  )
  email_alert = models.BooleanField(default=False)
  hipchat_alert = models.BooleanField(default=True)
  sms_alert = models.BooleanField(default=False)
  telephone_alert = models.BooleanField(
    default=False,
    help_text='Must be enabled, and check importance set to Critical, to receive telephone alerts.',
  )
  alerts_enabled = models.BooleanField(
    default=True,
    help_text='Alert when this service is not healthy.',
  )
  overall_status = models.TextField(default=PASSING_STATUS)
  old_overall_status = models.TextField(default=PASSING_STATUS)
  hackpad_id = models.TextField(
    null=True,
    blank=True,
    help_text='Alphanumeric id of Hackpad containing recovery information.'
  )

  class Meta:
    ordering = ['name']

  def __unicode__(self):
    return self.name

  def update_status(self):
    self.old_overall_status = self.overall_status
    # Only active checks feed into our calculation
    status_checks_failed_count = self.all_failing_checks().count()
    self.overall_status = self.most_severe(self.all_failing_checks())
    self.snapshot = ServiceStatusSnapshot(
      service=self,
      num_checks_active=self.active_status_checks().count(),
      num_checks_passing=self.active_status_checks().count()-status_checks_failed_count,
      num_checks_failing=status_checks_failed_count,
      overall_status=self.overall_status,
      time=timezone.now(),
    )
    self.snapshot.save()
    self.save()
    if not (self.overall_status == Service.PASSING_STATUS and self.old_overall_status == Service.PASSING_STATUS):
      self.alert()

  def most_severe(self, check_list):
    failures = [c.importance for c in check_list]
    if self.CRITICAL_STATUS in failures:
      return self.CRITICAL_STATUS
    if self.ERROR_STATUS in failures:
      return self.ERROR_STATUS
    if self.WARNING_STATUS in failures:
      return self.WARNING_STATUS
    return self.PASSING_STATUS

  @property
  def is_critical(self):
    """
    Break out separately because it's a bit of a pain to
    get wrong.
    """
    if self.old_overall_status != self.CRITICAL_STATUS and self.overall_status == self.CRITICAL_STATUS:
      return True
    return False

  def alert(self):
    if not self.alerts_enabled:
      return
    if self.overall_status != self.PASSING_STATUS:
      # Don't alert every time - only every 10 mins for errors and critical, and 120 mins for warnings
      if self.overall_status == self.WARNING_STATUS:
        if self.last_alert_sent and (timezone.now() - timedelta(minutes=120)) < self.last_alert_sent:
          return
      elif self.overall_status in (self.CRITICAL_STATUS, self.ERROR_STATUS):
        if self.last_alert_sent and (timezone.now() - timedelta(minutes=10)) < self.last_alert_sent:
          return
      self.last_alert_sent = timezone.now()
    else:
      self.last_alert_sent = None # We don't count "back to normal" as an alert
    self.save()
    self.snapshot.did_send_alert = True
    self.snapshot.save()
    # send_alert handles the logic of how exactly alerts should be handled
    send_alert(self, duty_officers=get_duty_officers())

  @property
  def recent_snapshots(self):
    snapshots = self.snapshots.filter(time__gt=(timezone.now() - timedelta(minutes=60*4)))
    snapshots = list(snapshots.values())
    for s in snapshots:
      s['time'] = time.mktime(s['time'].timetuple())
    return snapshots

  def active_status_checks(self):
    return self.status_checks.filter(active=True)

  def inactive_status_checks(self):
    return self.status_checks.filter(active=False)

  def all_passing_checks(self):
    return self.active_status_checks().filter(calculated_status=self.CALCULATED_PASSING_STATUS)

  def all_failing_checks(self):
    return self.active_status_checks().exclude(calculated_status=self.CALCULATED_PASSING_STATUS)

  def graphite_status_checks(self):
    return self.status_checks.filter(polymorphic_ctype__model='graphitestatuscheck')

  def http_status_checks(self):
    return self.status_checks.filter(polymorphic_ctype__model='httpstatuscheck')

  def jenkins_status_checks(self):
    return self.status_checks.filter(polymorphic_ctype__model='jenkinsstatuscheck')

  def active_graphite_status_checks(self):
    return self.graphite_status_checks().filter(active=True)

  def active_http_status_checks(self):
    return self.http_status_checks().filter(active=True)

  def active_jenkins_status_checks(self):
    return self.jenkins_status_checks().filter(active=True)


class ServiceStatusSnapshot(models.Model):
  service = models.ForeignKey(Service, related_name='snapshots')
  time = models.DateTimeField()
  num_checks_active = models.IntegerField(default=0)
  num_checks_passing = models.IntegerField(default=0)
  num_checks_failing = models.IntegerField(default=0)
  overall_status = models.TextField(default=Service.PASSING_STATUS)
  did_send_alert = models.IntegerField(default=False)

  def __unicode__(self):
    return u"%s: %s" % (self.service.name, self.overall_status)


class StatusCheck(PolymorphicModel):
  """
  Base class for polymorphic models. We're going to use
  proxy models for inheriting because it makes life much simpler,
  but this allows us to stick different methods etc on subclasses.

  You can work out what (sub)class a model is an instance of by accessing `instance.polymorphic_ctype.model`

  We are using django-polymorphic for polymorphism
  """

  # Common attributes to all
  name = models.TextField()
  active = models.BooleanField(
    default=True,
    help_text='If not active, check will not be used to calculate service status and will not trigger alerts.',
  )
  importance = models.CharField(
    max_length=30,
    choices=Service.IMPORTANCES,
    default=Service.ERROR_STATUS,
    help_text='Severity level of a failure. Critical alerts are for failures you want to wake you up at 2am, Errors are things you can sleep through but need to fix in the morning, and warnings for less important things.'
  )
  frequency = models.IntegerField(
    default=5,
    help_text='Minutes between each check.',
  )
  debounce = models.IntegerField(
    default=0,
    null=True,
    help_text='Number of successive failures permitted before check will be marked as failed. Default is 0, i.e. fail on first failure.'
  )
  created_by = models.ForeignKey(User)
  calculated_status = models.CharField(max_length=50, choices=Service.STATUSES, default=Service.CALCULATED_PASSING_STATUS, blank=True)
  last_run = models.DateTimeField(null=True)
  cached_health = models.TextField(editable=False, null=True)

  # Graphite checks
  metric = models.TextField(
    null=True,
    help_text='fully.qualified.name of the Graphite metric you want to watch. This can be any valid Graphite expression, including wildcards, multiple hosts, etc.',
  )
  check_type = models.CharField(
    choices=CHECK_TYPES,
    max_length=100,
    null=True,
  )
  value = models.TextField(
    null=True,
    help_text='If this expression evaluates to true, the check will fail (possibly triggering an alert).',
  )
  expected_num_hosts = models.IntegerField(
    default=0,
    null=True,
    help_text='The minimum number of data series (hosts) you expect to see.',
  )

  # HTTP checks
  endpoint = models.TextField(
    null=True,
    help_text='HTTP(S) endpoint to poll.',
  )
  username = models.TextField(
    blank=True,
    null=True,
    help_text='Basic auth username.',
  )
  password = models.TextField(
    blank=True,
    null=True,
    help_text='Basic auth password.',
  )
  text_match = models.TextField(
    blank=True,
    null=True,
    help_text='Regex to match against source of page.',
  )
  status_code = models.TextField(
    default=200,
    null=True,
    help_text='Status code expected from endpoint.'
  )
  timeout = models.IntegerField(
    default=30,
    null=True,
    help_text='Time out after this many seconds.',
  )

  # Jenkins checks
  max_queued_build_time = models.IntegerField(
    null=True,
    blank=True,
    help_text='Alert if build queued for more than this many minutes.',
  )

  class Meta(PolymorphicModel.Meta):
    ordering = ['name']

  def __unicode__(self):
    return self.name

  def recent_results(self):
    return self.statuscheckresult_set.all().order_by('-time_complete')[:10]

  def last_result(self):
    try:
      return self.recent_results()[0]
    except:
      return None

  def run(self):
    raise NotImplementedError('Subclasses should implement')

  def save(self, *args, **kwargs):
    recent_results = self.recent_results()
    if calculate_debounced_passing(recent_results, self.debounce):
      self.calculated_status = Service.CALCULATED_PASSING_STATUS
    else:
      self.calculated_status = Service.CALCULATED_FAILING_STATUS
    self.cached_health = serialize_recent_results(recent_results)
    super(StatusCheck, self).save(*args, **kwargs)


class GraphiteStatusCheck(StatusCheck):

  class Meta(StatusCheck.Meta):
    proxy = True

  @property
  def check_category(self):
    return "Metric check"

  def format_error_message(self, failure_value, actual_hosts):
    """
    A summary of why the check is failing for inclusion in hipchat, sms etc
    Returns something like:
    "5.0 > 4 | 1/2 hosts"
    """
    if failure_value is None:
      return "Failed to get metric from Graphite"
    hosts_string = ''
    if self.expected_num_hosts > 0:
      hosts_string = ' | %s/%s hosts' % (actual_hosts, self.expected_num_hosts)
    return "%0.1f %s %0.1f%s" % (
      failure_value,
      self.check_type,
      float(self.value),
      hosts_string
    )

  def run(self):
    start = timezone.now()
    series = parse_metric(self.metric, mins_to_check=self.frequency)
    failure_value = None
    if series['error']:
      failed = True
    else:
      failed = None

    finish = timezone.now()
    result = StatusCheckResult(
      check=self,
      time=start,
      time_complete=finish,
    )
    if series['num_series_with_data'] > 0:
      result.average_value = series['average_value']
      if self.check_type == '<':
        failed = float(series['min']) < float(self.value)
        if failed:
          failure_value = series['min']
      elif self.check_type == '<=':
        failed = float(series['min']) <= float(self.value)
        if failed:
          failure_value = series['min']
      elif self.check_type == '>':
        failed = float(series['max']) > float(self.value)
        if failed:
          failure_value = series['max']
      elif self.check_type == '>=':
        failed = float(series['max']) >= float(self.value)
        if failed:
          failure_value = series['max']
      elif self.check_type == '==':
        failed = float(self.value) in series['all_values']
        if failed:
          failure_value = float(self.value)
      else:
        raise Exception('Check type %s not supported' % self.check_type)

    if series['num_series_with_data'] < self.expected_num_hosts:
      failed = True

    try:
      result.raw_data = json.dumps(series['raw'])
    except:
      result.raw_data = series['raw']
    result.succeeded = not failed
    if not result.succeeded:
      result.error = self.format_error_message(
        failure_value,
        series['num_series_with_data'],
      )

    result.actual_hosts = series['num_series_with_data']
    result.failure_value = failure_value
    result.save()

    self.last_run = finish
    super(GraphiteStatusCheck, self).save()


class HttpStatusCheck(StatusCheck):

  class Meta(StatusCheck.Meta):
    proxy = True

  @property
  def check_category(self):
    return "HTTP check"

  def run(self):
    start = timezone.now()
    result = StatusCheckResult(check=self, time=start)
    auth = (self.username, self.password)
    try:
      resp = requests.get(
        self.endpoint,
        timeout=self.timeout,
        verify=True,
        auth=auth
      )
    except requests.RequestException, exc:
      result.error = 'Request error occurred: %s' % (exc,)
      result.succeeded = False
    else:
      if self.status_code and resp.status_code != int(self.status_code):
        result.error = 'Wrong code: got %s (expected %s)' % (resp.status_code, int(self.status_code))
        result.succeeded = False
        result.raw_data = resp.content
      elif self.text_match:
        if not re.search(self.text_match, resp.content):
          result.error = 'Failed to find match regex [%s] in response body' % self.text_match
          result.raw_data = resp.content
          result.succeeded = False
        else:
          result.succeeded = True
      else:
        result.succeeded = True

    finish = timezone.now()
    result.time_complete = finish
    result.save()
    self.last_run = finish
    super(HttpStatusCheck, self).save()


class JenkinsStatusCheck(StatusCheck):

  class Meta(StatusCheck.Meta):
    proxy = True

  @property
  def check_category(self):
    return "Jenkins check"

  @property
  def failing_short_status(self):
    return 'Job failing on Jenkins'

  def run(self):
    start = timezone.now()
    result = StatusCheckResult(
      check=self,
      time=start,
    )
    try:
      status = get_job_status(self.name)
      active = status['active']
    except requests.HTTPError:
      # Fail if there's a 404 - the job is misconfigured probably
      result.error = 'Job %s not found on Jenkins' % self.name
      result.succeeded = False
      finish = timezone.now()
      result.time_complete = finish
      result.save()
      self.last_run = finish
      super(JenkinsStatusCheck, self).save()
      return
    except:
      # If something else goes wrong, we will *not* fail - otherwise
      # a lot of services seem to fail all at once.
      # Ugly to do it here but...
      finish = timezone.now()
      result.error = 'Error fetching from Jenkins'
      result.succeeded = True
      result.time_complete = finish
      result.save()
      self.last_run = finish
      super(JenkinsStatusCheck, self).save()
      return
    if not active:
      # We will fail if the job has been disabled
      result.error = 'Job disabled on Jenkins' % self.name
      result.succeeded = False
    else:
      if self.max_queued_build_time and status['blocked_build_time']:
        if status['blocked_build_time'] > self.max_queued_build_time*60:
          result.succeeded = False
          result.error = 'Job "%s" has blocked build waiting for %ss (> %sm)' % (
            self.name,
            int(status['blocked_build_time']),
            self.max_queued_build_time
          )
        else:
          result.succeeded = status['succeeded']
      else:
        result.succeeded = status['succeeded']
      if not status['succeeded']:
        if result.error:
          result.error +='; Job "%s" failing on Jenkins' % self.name
        else:
          result.error = 'Job "%s" failing on Jenkins' % self.name
        result.raw_data = status

    finish = timezone.now()
    result.time_complete = finish
    result.save()
    self.last_run = finish
    super(JenkinsStatusCheck, self).save()


class StatusCheckResult(models.Model):
  """
  We use the same StatusCheckResult model for all check types,
  because really they are not so very different.

  Checks don't have to use all the fields, so most should be
  nullable
  """
  check = models.ForeignKey(StatusCheck)
  time = models.DateTimeField(null=False)
  time_complete = models.DateTimeField(null=True)
  raw_data = models.TextField(null=True)
  succeeded = models.BooleanField(default=False)
  error = models.TextField(null=True)

  def __unicode__(self):
    return '%s: %s @%s' % (self.status, self.check.name, self.time)

  @property
  def status(self):
    if self.succeeded:
      return 'succeeded'
    else:
      return 'failed'

  @property
  def took(self):
    try:
      return (self.time_complete - self.time).microseconds / 1000
    except:
      return None

  @property
  def short_error(self):
    snippet_len = 30
    if len(self.error) > snippet_len:
      return u"%s..." % self.error[:snippet_len-3]
    else:
      return self.error


class UserProfile(models.Model):
  user = models.OneToOneField(User, related_name='profile')
  mobile_number = models.CharField(max_length=20, blank=True, default='')
  hipchat_alias = models.CharField(max_length=50, blank=True, default='')
  fallback_alert_user = models.BooleanField(default=False)

  def __unicode__(self):
    return 'User profile: %s' % self.user.username

  def save(self, *args, **kwargs):
    if self.mobile_number.startswith('+'):
      self.mobile_number = self.mobile_number[1:]
    # Enforce uniqueness
    if self.fallback_alert_user:
      profiles = UserProfile.objects.exclude(id=self.id)
      profiles.update(fallback_alert_user=False)
    return super(UserProfile, self).save(*args, **kwargs)

  @property
  def prefixed_mobile_number(self):
    return '+%s' % self.mobile_number


class Shift(models.Model):
  start = models.DateTimeField()
  end = models.DateTimeField()
  user = models.ForeignKey(User)
  uid = models.TextField()
  deleted = models.BooleanField(default=False)

  def __unicode__(self):
    deleted = ''
    if self.deleted:
      deleted = ' (deleted)'
    return "%s: %s to %s%s" % (self.user.username, self.start, self.end, deleted)


def get_duty_officers(at_time=None):
  """Returns a list of duty officers for a given time or now if none given"""
  duty_officers = []
  if not at_time:
    at_time = timezone.now()
  current_shifts = Shift.objects.filter(
    deleted=False,
    start__lt=at_time,
    end__gt=at_time,
  )
  if current_shifts:
    duty_officers = [shift.user for shift in current_shifts]
    return duty_officers
  else:
    try:
      u = UserProfile.objects.get(fallback_alert_user=True)
      return [u]
    except UserProfile.DoesNotExist:
      return []


def update_shifts():
  events = get_events()
  users = User.objects.filter(is_active=True)
  user_lookup = {}
  for u in users:
    user_lookup[u.username.lower()] = u
  future_shifts = Shift.objects.filter(start__gt=timezone.now())
  future_shifts.update(deleted=True)

  for event in events:
    e = event['summary'].lower().strip()
    if e in user_lookup:
      user = user_lookup[e]
      try:
        s = Shift.objects.get(uid=event['uid'])
      except Shift.DoesNotExist:
        s = Shift(uid=event['uid'])
      s.start = event['start']
      s.end = event['end']
      s.user = user
      s.deleted = False
      s.save()
