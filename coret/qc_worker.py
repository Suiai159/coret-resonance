"""Independent Qt analysis worker; does not own or touch the serial controller."""
from PySide6.QtCore import QObject, Signal, Slot
from .qc_engine import QCEngine


class QCWorker(QObject):
    updated=Signal(int,dict)

    def __init__(self):
        super().__init__()
        self.generation=-1
        self.engine=None

    def failure(self,generation,error,final=False):
        self.updated.emit(generation,{'result':'INVALID_DATA','reason':'QC_ANALYSIS_ERROR',
                                     'message':'质检计算失败，不能放行：'+str(error),'final':final})

    @Slot(int,object)
    def reset(self,generation,reference=None):
        self.generation=generation
        try:
            if isinstance(reference,dict) and '_load_error' in reference:
                raise ValueError(reference['_load_error'])
            self.engine=QCEngine(reference)
        except Exception as e:
            self.engine=None;self.failure(generation,e)

    @Slot(int,dict,object)
    def samples(self,generation,metadata,delta):
        if generation!=self.generation or self.engine is None:return
        try:
            result=self.engine.feed(metadata,delta)
            if result is not None:self.updated.emit(generation,result)
        except Exception as e:
            self.engine.error(str(e));self.failure(generation,e)

    @Slot(int,dict,bool)
    def finish(self,generation,capture,replay):
        if generation!=self.generation:return
        try:
            if replay and self.engine is not None:self.engine.reset()
            if self.engine is None:raise ValueError('质检配置未成功加载')
            self.updated.emit(generation,self.engine.finish(capture))
        except Exception as e:self.failure(generation,e,True)
