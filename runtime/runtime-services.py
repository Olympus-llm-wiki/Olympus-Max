#!/usr/bin/env python3
"""Compose entry points over the pinned native application factories."""
import sys


def proxy_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location('olympus_model_proxy', '/opt/olympus/model_proxy.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(mode):
    if mode == 'worker':
        from hindsight_api.worker import main as native
        from hindsight_api.api.http import create_app
        from hindsight_api.config import get_config
        module = proxy_module()
        original = native.create_worker_app

        def with_model_routes(poller, memory):
            app = original(poller, memory)
            # Mounted sub-app lifespan is not run. The worker already owns the
            # initialized engine, poller and shutdown; never start a second one.
            model_app = create_app(memory, initialize_memory=False)
            config = get_config()
            fields = ('llm_provider', 'retain_llm_provider', 'llm_model', 'retain_llm_model',
                      'llm_reasoning_effort', 'retain_llm_reasoning_effort')
            profile = {'schema': 1, 'role': 'worker', **{key: getattr(config, key, None) for key in fields}}
            model_app.add_middleware(module.ExecutorConfig, profile=profile)
            app.mount('/model', model_app)
            return app

        native.create_worker_app = with_model_routes
        sys.argv = ['hindsight-worker']
    elif mode == 'api':
        module = proxy_module()
        from hindsight_api import main as native
        from hindsight_api.api import create_app

        def with_model_proxy(*args, **kwargs):
            app = create_app(*args, **kwargs)
            app.add_middleware(module.ModelProxy)
            return app

        native.create_app = with_model_proxy
        sys.argv = ['hindsight-api']
    else:
        raise SystemExit('invalid_runtime_role')
    native.main()


if __name__ == '__main__':
    main(sys.argv[1])
