import threading

from django.core.cache import cache

from smartfields.utils import ProcessingError, VALUE_NOT_SET

__all__ = [
    'FieldManager',
]

class AsyncHandler(threading.Thread):

    def __init__(self, manager, instance):
        self.manager, self.instance = manager, instance
        super(AsyncHandler, self).__init__()

    def get_progress_setter(self, multiplier, index):
        def progress_setter(processor, progress):
            try:
                progress = multiplier * (index + progress)
            except TypeError as e:
                raise ProcessingError("Problem setting progress: %s" % e)
            self.manager.set_status(self.instance, {
                'task': getattr(processor, 'task', 'processing'),
                'task_name': getattr(processor, 'task_name', "Processing"),
                'state': 'processing',
                'progress': progress
            })
        return progress_setter

    def run(self):
        dependencies = list(filter(lambda d: d.async, self.manager.dependencies))
        multiplier = 1.0/len(dependencies)
        try:
            should_save = False
            for idx, d in enumerate(dependencies):
                should_save = should_save or d._dependee is not None
                self.manager._process(
                    self.instance, d, 
                    progress_setter=self.get_progress_setter(multiplier, idx)
                )
            if should_save:
                self.instance.save()
            self.manager.set_status(self.instance, {'state': 'ready'})
        except ProcessingError: pass


class FieldManager(object):
    _stashed_value = VALUE_NOT_SET
    
    def __init__(self, field, dependencies):
        self.field = field
        self.dependencies = dependencies
        self.has_async = False
        self.has_processors = False
        for d in self.dependencies:
            d.set_field(self.field)
            self.has_async = self.has_async or d.async
            self.has_processors = self.has_processors or bool(d._processor)

    @property
    def has_stashed_value(self):
        return self._stashed_value is not VALUE_NOT_SET

    def stash_previous_value(self, value):
        if self._stashed_value is VALUE_NOT_SET:
            self._stashed_value = value

    def handle(self, instance, event, *args, **kwargs):
        if event == 'pre_init':
            instance.__dict__[self.field.name] = VALUE_NOT_SET
            field_value = None
        else:
            field_value = self.field.value_from_object(instance)
            if event == 'post_init':
                # mark manager for processing by stashing default value
                if instance.pk is None and self.field.name in kwargs:
                    self.stash_previous_value(self.field.get_default())
            elif event == 'post_delete' and field_value:
                self.delete_value(field_value)
        for d in self.dependencies:
            d.handle(instance, field_value, event, *args, **kwargs)

    def _process(self, instance, dependency, progress_setter=None):
        # process single dependency
        try:
            field_value = self.field.value_from_object(instance)
            dependency.process(
                instance, field_value, progress_setter=progress_setter)
        except BaseException as e:
            self.failed_processing(instance, e)
            raise

    def failed_processing(self, instance, error=None):
        self.restore_stash(instance)
        if error is not None:
            self.set_error_status(instance, "%s: %s" % (type(error).__name__, str(error)))

    def finished_processing(self, instance):
        if self.has_stashed_value:
            self.cleanup_stash()
        self.set_status(instance, {'state': 'ready'})
        
    def cleanup(self, instance):
        for d in self.dependencies:
            d.cleanup(instance)

    def delete_value(self, value):
        if hasattr(value, 'delete') and hasattr(value, 'field') \
           and value and not value.field.keep_orphans:
                value.delete(instance_update=False)
    
    def cleanup_stash(self):
        self.delete_value(self._stashed_value)
        self._stashed_value = VALUE_NOT_SET
        for d in self.dependencies:
            if d.has_stashed_value:
                d.cleanup_stash()

    def restore_stash(self, instance):
        if self.has_stashed_value:
            self.delete_value(self.field.value_from_object(instance))
            instance.__dict__[self.field.name] = self._stashed_value
            self._stashed_value =  VALUE_NOT_SET
        for d in self.dependencies:
            if d.has_stashed_value:
                d.restore_stash(instance)

    def process(self, instance, force=False):
        """ Processing is triggered by field's pre_save method. It will be executed if field's
        value has been changed (known through descriptor and stashing logic) or if model 
        instance has never been saved before, i.e. no pk set, because there is a chance
        that field was initialized through model's `__init__`, hence no value was stashed."""
        if self.has_processors and (force or self.has_stashed_value):
            self.set_status(instance, {'state': 'busy'})
            try:
                if self.has_async:
                    for d in filter(lambda d: not d.async, self.dependencies):
                        self._process(instance, d)
                    async_handler = AsyncHandler(self, instance)
                    async_handler.start()
                else:
                    for d in self.dependencies:
                        self._process(instance, d)
                    self.finished_processing(instance)
            except ProcessingError: pass

    def get_status_key(self, instance):
        """Generates a key used to set a status on a field"""
        key_id = "inst_%s" % id(instance) if instance.pk is None else instance.pk
        return "%s.%s-%s-%s" % (instance._meta.app_label,
                                instance._meta.model_name,
                                key_id,
                                self.field.name)

    def _get_status(self, instance, status_key=None):
        status_key = status_key or self.get_status_key(instance)
        return status_key, cache.get(status_key, None)

    def get_status(self, instance):
        """Retrives a status of a field from cache. Fields in state 'error' and
        'complete' will not retain the status after the call.

        """
        status = {
            'app_label': instance._meta.app_label,
            'model_name': instance._meta.model_name,
            'pk': instance.pk,
            'field_name': self.field.name,
            'state': 'ready'
        }
        status_key, current_status = self._get_status(instance)
        if current_status is not None:
            status.update(current_status)
            if status['state'] in ['complete', 'error']:
                cache.delete(status_key)
        return status

    def set_status(self, instance, status):
        """Sets the field status for up to 5 minutes."""
        status_key = self.get_status_key(instance)
        cache.set(status_key, status, timeout=300)

    def set_error_status(self, instance, error):
        self.set_status(instance, {
            'state': 'error',
            'messages': [error]
        })

    def contribute_to_model(self, model, name):
        model._smartfields_managers[name] = self
        for d in self.dependencies:
            d.contribute_to_model(model)